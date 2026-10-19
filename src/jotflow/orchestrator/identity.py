"""`/jotflow link`: a person links their own Slack account to a GitHub login (#35).

Assignees work only through these links (#15), so this is how a proposal can
name someone. Links are self-declared in v1, so the store keeps them
unverified, and every card marks such a login "(unverified)" for everyone,
approvers included. A person can link only themselves; the command takes no
other user. Guests and external Slack Connect users can't link at all.

Nothing but the Slack id, the login and the time is kept. The log names the
Slack id and what happened, never the login of someone else or any text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from jotflow.orchestrator.people import Person
from jotflow.store.base import ConflictError, Store, check_github_login

log = logging.getLogger("jotflow.identity")

_PROFILE_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/?#\s]+)/?$", re.IGNORECASE)

USAGE = (
    "Link your GitHub login so proposals can assign you: `/jotflow link your-login`. "
    "`/jotflow link` shows yours, and `/jotflow unlink` removes it."
)
NOT_ALLOWED = "Only people in this workspace who are not guests can link a GitHub login."
NOT_A_LOGIN = (
    "That isn't a GitHub login. A login is 1 to 39 letters, digits or single hyphens, "
    "such as octo-cat, and doesn't start or end with a hyphen."
)
TAKEN = (
    "Someone in this workspace has already linked that GitHub login. If it's yours, ask "
    "them, or whoever runs JotFlow, to unlink it."
)
NOTHING_LINKED = "You haven't linked a GitHub login. " + USAGE


def _linked(login: str) -> str:
    return (
        f"Linked to GitHub login {login}. Proposals can now assign you. Cards show it as "
        "unverified, because JotFlow can't check GitHub itself."
    )


def _current(login: str) -> str:
    return f"You're linked to GitHub login {login}, shown as unverified on cards."


def _unlinked(login: str) -> str:
    return f"Unlinked GitHub login {login}. Proposals can no longer assign you."


class PeopleLookup(Protocol):
    async def person(self, user_id: str) -> Person: ...


@dataclass(frozen=True)
class LinkReply:
    text: str


def clean_login(raw: str) -> str | None:
    """The login in what someone typed: a bare login, ``@login``, or a profile link."""
    value = raw.strip()
    if match := _PROFILE_URL.match(value):
        value = match.group(1)
    value = value.removeprefix("@")
    try:
        return check_github_login(value)
    except ValueError:
        return None


class LinkCommand:
    """``/jotflow link``, ``/jotflow link <login>`` and ``/jotflow unlink``."""

    def __init__(self, store: Store, people: PeopleLookup) -> None:
        self._store = store
        self._people = people

    async def handle(self, user_id: str, verb: str, argument: str) -> LinkReply:
        try:
            person = await self._people.person(user_id)
        except Exception:
            log.warning("could not look up a Slack user for /jotflow %s", verb)
            return LinkReply(NOT_ALLOWED)  # fail closed
        if person.guest or person.external:
            log.info("/jotflow %s refused for user %s", verb, user_id)
            return LinkReply(NOT_ALLOWED)
        if verb == "unlink":
            return await self._unlink(user_id)
        if not argument.strip():
            current = await self._store.get_identity(user_id)
            return LinkReply(NOTHING_LINKED if current is None else _current(current.github_login))
        login = clean_login(argument)
        if login is None:
            return LinkReply(NOT_A_LOGIN)
        try:
            link = await self._store.link_identity(user_id, login)
        except ConflictError:
            log.info("user %s tried to link a GitHub login someone else holds", user_id)
            return LinkReply(TAKEN)
        log.info("user %s linked a GitHub login", user_id)
        return LinkReply(_linked(link.github_login))

    async def _unlink(self, user_id: str) -> LinkReply:
        current = await self._store.get_identity(user_id)
        if current is None or not await self._store.unlink_identity(user_id):
            return LinkReply(NOTHING_LINKED)
        log.info("user %s unlinked their GitHub login", user_id)
        return LinkReply(_unlinked(current.github_login))
