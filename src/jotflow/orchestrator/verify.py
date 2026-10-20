"""`/jotflow verify`: prove control of a GitHub login with a one-time code (#195).

A person asks for a code, puts it in their GitHub profile bio or as a public
gist's description, and asks JotFlow to check. The check runs in the drafting
runner, anonymously, so JotFlow's own container stays off GitHub.

- ``/jotflow verify`` gives a code for the caller's own linked login.
- ``/jotflow verify your-login`` gives a code to claim a login, even one someone
  else has linked. Proof moves the link to the prover.
- ``/jotflow verify check`` asks JotFlow to check now.

The code is shown once, only to the person who asked. The store keeps its
digest, bound to that Slack user and login, for 30 minutes. It's single use,
checked at most 5 times, at most once every 30 seconds. A rate-limited GitHub
answer is "try again later", never "not verified", and doesn't count. Logs name
the Slack id and the outcome, never the code.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from jotflow.orchestrator.identity import NOT_ALLOWED, Notifier, PeopleLookup, clean_login
from jotflow.orchestrator.launch import RunnerError, VerifyAnswer
from jotflow.proof_codes import code_digest, new_code
from jotflow.store.base import IdentityCode, Store, utc_now

log = logging.getLogger("jotflow.identity")

CODE_LIFETIME = timedelta(minutes=30)
MAX_CHECKS = 5
CHECK_COOLDOWN_SECONDS = 30.0

NO_LINK = (
    "You haven't linked a GitHub login. Link one with /jotflow link your-github-login, "
    "or claim one with /jotflow verify your-github-login."
)
NO_PENDING = "You have no code waiting. Start with /jotflow verify."
EXPIRED = "Your code expired. Start again with /jotflow verify."
TOO_SOON = "Wait half a minute between checks, then try /jotflow verify check again."
TOO_MANY = (
    "That code was checked 5 times without a match, so it's withdrawn. "
    "Start again with /jotflow verify."
)
RATE_LIMITED = (
    "GitHub is limiting requests right now, so nothing was checked. "
    "Try /jotflow verify check again in a few minutes."
)
UNAVAILABLE = (
    "JotFlow couldn't reach GitHub through its agent container just now, so nothing was "
    "checked. Try /jotflow verify check again in a few minutes."
)
NO_RUNNER = "Verifying needs JotFlow's agent containers, and they aren't on here."
NOT_A_LOGIN = "That isn't a GitHub login. Try /jotflow verify your-github-login."
DISPLACED_NOTE = (
    "Someone proved they control the GitHub login you had linked, so it's no longer "
    "linked to your Slack account. If it's yours, prove it with /jotflow verify "
    "your-github-login."
)


def _issued(login: str, code: str) -> str:
    return (
        f"To verify GitHub login {login}, add this code to your GitHub profile bio, or as "
        f"the description of a public gist, within 30 minutes: `{code}`\n"
        "Then type /jotflow verify check. You can remove the code once you're verified."
    )


def _already(login: str) -> str:
    return f"Your GitHub login {login} is already verified."


def _not_found(login: str) -> str:
    return (
        f"JotFlow didn't find your code on GitHub login {login}'s profile bio or recent "
        "public gists. Check it's saved, then try /jotflow verify check again."
    )


def _verified(login: str) -> str:
    return f"Verified: GitHub login {login} is yours, and cards no longer mark it unverified."


class Verifier(Protocol):
    async def verify(self, login: str, code_digest: str) -> VerifyAnswer: ...


class VerifyCommand:
    """``/jotflow verify``, ``/jotflow verify <login>`` and ``/jotflow verify check``."""

    def __init__(
        self,
        store: Store,
        people: PeopleLookup,
        verifier: Callable[[], Verifier | None],
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        code: Callable[[], str] = new_code,
    ) -> None:
        self._store = store
        self._people = people
        self._verifier = verifier
        self._notifier = notifier
        self._clock = clock
        self._monotonic = monotonic
        self._code = code
        self._last_check: dict[str, float] = {}

    async def handle(self, user_id: str, argument: str) -> str:
        try:
            person = await self._people.person(user_id)
        except Exception:
            log.warning("could not look up a Slack user for /jotflow verify")
            return NOT_ALLOWED  # fail closed
        if person.guest or person.external:
            return NOT_ALLOWED
        word = argument.strip()
        if word.lower() == "check":
            return await self._check(user_id)
        if not word:
            link = await self._store.get_identity(user_id)
            if link is None:
                return NO_LINK
            if link.verified:
                return _already(link.github_login)
            return await self._issue(user_id, link.github_login)
        login = clean_login(word)
        if login is None:
            return NOT_A_LOGIN
        mine = await self._store.get_identity(user_id)
        if mine is not None and mine.verified and mine.github_login.casefold() == login.casefold():
            return _already(mine.github_login)
        return await self._issue(user_id, login)

    async def _issue(self, user_id: str, login: str) -> str:
        if self._verifier() is None:
            return NO_RUNNER
        code = self._code()
        await self._store.start_verification(
            IdentityCode(user_id, login, code_digest(code), self._clock() + CODE_LIFETIME)
        )
        log.info("user %s asked for a GitHub verification code", user_id)
        return _issued(login, code)

    async def _check(self, user_id: str) -> str:
        pending = await self._store.get_verification(user_id)
        if pending is None:
            return NO_PENDING
        if self._clock() >= pending.expires_at:
            await self._store.drop_verification(user_id)
            return EXPIRED
        now = self._monotonic()
        last = self._last_check.get(user_id)
        if last is not None and now - last < CHECK_COOLDOWN_SECONDS:
            return TOO_SOON
        verifier = self._verifier()
        if verifier is None:
            return NO_RUNNER
        self._last_check[user_id] = now
        try:
            answer = await verifier.verify(pending.github_login, pending.code_digest)
        except RunnerError as failed:
            log.warning("a GitHub verification for user %s didn't run: %s", user_id, failed)
            return UNAVAILABLE
        if answer.rate_limited:
            return RATE_LIMITED  # doesn't count toward the code's checks
        same = answer.login is not None and answer.login.casefold() == (
            pending.github_login.casefold()
        )
        if not answer.verified or not same or answer.github_id is None or answer.login is None:
            checks = await self._store.count_verification_check(user_id)
            log.info("user %s's GitHub verification found no code (%d)", user_id, checks)
            if checks >= MAX_CHECKS:
                await self._store.drop_verification(user_id)
                return TOO_MANY
            return _not_found(pending.github_login)
        done = await self._store.finish_verification(
            user_id, github_login=answer.login, github_id=answer.github_id
        )
        log.info("user %s verified their GitHub login", user_id)
        if done.displaced is not None:
            await self._tell(done.displaced)
        return _verified(done.link.github_login)

    async def _tell(self, user_id: str) -> None:
        try:
            await self._notifier.tell(user_id, DISPLACED_NOTE)
        except Exception as exc:
            log.warning("could not tell user %s their link moved (%s)", user_id, type(exc).__name__)


VerifyHandler = Callable[[str, str], Awaitable[str]]
