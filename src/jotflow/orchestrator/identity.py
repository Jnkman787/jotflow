"""`/jotflow link`: a person links their own Slack account to a GitHub login (#35).

Assignees work only through these links (#15), so this is how a proposal can
name someone. A link starts self-declared, so the store keeps it unverified,
and every card marks such a login "(unverified)" for everyone, approvers
included, until the person proves the account is theirs with ``/jotflow
verify`` (#195, :mod:`jotflow.orchestrator.verify`). A person can link only
themselves. Guests and external Slack Connect users can't link at all.

An approver, or someone in ``[identity] admins``, can remove someone else's
link with ``/jotflow unlink <member-id>`` (#173), for a login claimed by the
wrong person. The target is named by Slack member id, never by name. The
person is told privately, and the store records who removed whose link and
when, never the login or any text. Whoever runs JotFlow can do the same from
the host with ``jotflow identity unlink``.

Nothing but the Slack id, the login and the time is kept. The log names the
Slack ids and what happened, never the login of someone else or any text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from jotflow.orchestrator.people import Person
from jotflow.policy import Policy
from jotflow.store.base import ConflictError, Store, check_github_login
from jotflow.text import without_unsafe_chars

log = logging.getLogger("jotflow.identity")

_PROFILE_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/?#\s]+)/?$", re.IGNORECASE)

USAGE = (
    "Link your GitHub login so proposals can assign you: `/jotflow link your-login`. "
    "`/jotflow link` shows yours, and `/jotflow unlink` removes it. Approvers can remove "
    "someone else's with `/jotflow unlink their-member-id`."
)
NOT_ALLOWED = "Only people in this workspace who are not guests can link a GitHub login."
NOT_A_LOGIN = (
    "That isn't a GitHub login. A login is 1 to 39 letters, digits or single hyphens, "
    "such as octo-cat, and doesn't start or end with a hyphen."
)
TAKEN = (
    "Someone in this workspace has already linked that GitHub login. If it's yours, ask "
    "them, an approver, or whoever runs JotFlow to unlink it."
)
NOTHING_LINKED = "You haven't linked a GitHub login. " + USAGE

_BARE_ID = re.compile(r"^[UW][A-Z0-9]{2,20}$")
_MENTION = re.compile(r"^<@([UW][A-Z0-9]{2,20})(?:\|[^>]*)?>$")
NOT_A_MEMBER_ID = (
    "To unlink someone else, give their Slack member ID, such as U0123ABCD: open their "
    "profile, choose the three dots, then Copy member ID. JotFlow never goes by names."
)
NOT_AN_ADMIN = (
    "Only approvers, or the admins listed in jotflow.toml, can unlink someone else. "
    "Ask one of them, or whoever runs JotFlow."
)
UNKNOWN_PERSON = "an unknown user"


def member_id(raw: str) -> str | None:
    """A Slack member id, bare or as a mention, exactly as Slack writes it. Never a name.

    Not case-folded: Slack ids are upper case, and folding would turn a short
    lowercase name such as ``uma`` into an id-shaped ``UMA``.
    """
    value = raw.strip()
    if match := _MENTION.match(value):
        return match.group(1)
    return value if _BARE_ID.match(value) else None


def _plain(name: str) -> str:
    """A display name safe as plain text and as mrkdwn: no markup, links or mentions.

    ``<``, ``>`` and ``&`` are dropped rather than escaped, so the same text reads
    right in a plain_text block and in a mrkdwn reply.
    """
    cleaned = without_unsafe_chars(name)
    cleaned = cleaned.replace("<", "").replace(">", "").replace("&", "").strip()
    return " ".join(cleaned.split())[:80] or UNKNOWN_PERSON


def _removed_other(name: str, user_id: str) -> str:
    return (
        f"Unlinked the GitHub login of {name} ({user_id}). Proposals can no longer assign "
        "them, and JotFlow told them privately."
    )


def _removed_untold(name: str, user_id: str) -> str:
    return (
        f"Unlinked the GitHub login of {name} ({user_id}). Proposals can no longer assign "
        "them. JotFlow couldn't message them, so please tell them yourself."
    )


def _nothing_to_remove(name: str, user_id: str) -> str:
    return f"{name} ({user_id}) has no linked GitHub login."


def removal_note(by: str | None) -> str:
    """What the person whose link was removed is told."""
    who = "Whoever runs JotFlow" if by is None else by
    return (
        f"{who} removed the GitHub login linked to your Slack account. Proposals can no "
        "longer assign you. If the login is yours, link it again with /jotflow link "
        "your-github-login."
    )


def _linked(login: str) -> str:
    return (
        f"Linked to GitHub login {login}. Proposals can now assign you. Cards show it as "
        "unverified until you prove it's yours with /jotflow verify."
    )


def _current(login: str, verified: bool) -> str:
    if verified:
        return f"You're linked to GitHub login {login}, verified."
    return (
        f"You're linked to GitHub login {login}, shown as unverified on cards. "
        "/jotflow verify proves it's yours."
    )


def _unlinked(login: str) -> str:
    return f"Unlinked GitHub login {login}. Proposals can no longer assign you."


class PeopleLookup(Protocol):
    async def person(self, user_id: str) -> Person: ...


class NameLookup(Protocol):
    async def name(self, user_id: str) -> str | None: ...


class Notifier(Protocol):
    async def tell(self, user_id: str, text: str) -> None: ...


class SlackNote:
    """A private note in the app's Messages tab, which the manifest makes read-only."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def tell(self, user_id: str, text: str) -> None:
        await self._client.chat_postMessage(
            channel=user_id,
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "plain_text", "text": text, "emoji": False}}
            ],
            unfurl_links=False,
            unfurl_media=False,
        )


async def remove_link(
    store: Store, notifier: Notifier, user_id: str, *, removed_by: str, by_name: str | None
) -> tuple[bool, bool]:
    """Remove ``user_id``'s link as ``removed_by`` and tell them.

    Returns whether a link was removed, and whether they were told.
    """
    if not await store.remove_identity(user_id, removed_by=removed_by):
        return False, False
    log.info("user %s's GitHub link was removed by %s", user_id, removed_by)
    try:
        await notifier.tell(user_id, removal_note(by_name))
    except Exception as exc:
        log.warning(
            "could not tell user %s their link was removed (%s)", user_id, type(exc).__name__
        )
        return True, False
    return True, True


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

    def __init__(
        self,
        store: Store,
        people: PeopleLookup,
        *,
        policy: Policy | None = None,
        names: NameLookup | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._store = store
        self._people = people
        self._policy = policy
        self._names = names
        self._notifier = notifier

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
            if argument.strip():
                return await self._unlink_other(user_id, argument)
            return await self._unlink(user_id)
        if not argument.strip():
            current = await self._store.get_identity(user_id)
            return LinkReply(
                NOTHING_LINKED
                if current is None
                else _current(current.github_login, current.verified)
            )
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

    async def _name(self, user_id: str) -> str:
        if self._names is None:
            return UNKNOWN_PERSON
        try:
            name = await self._names.name(user_id)
        except Exception:
            name = None
        return _plain(name) if name else UNKNOWN_PERSON

    async def _unlink_other(self, actor: str, argument: str) -> LinkReply:
        target = member_id(argument)
        if target is None:
            return LinkReply(NOT_A_MEMBER_ID)
        if target == actor:
            return await self._unlink(actor)
        if (
            self._policy is None
            or self._notifier is None
            or not self._policy.may_unlink_others(actor)
        ):
            log.info("user %s may not unlink someone else", actor)
            return LinkReply(NOT_AN_ADMIN)
        name = await self._name(target)
        removed, told = await remove_link(
            self._store,
            self._notifier,
            target,
            removed_by=actor,
            by_name=None
            if (actor_name := await self._name(actor)) == UNKNOWN_PERSON
            else actor_name,
        )
        if not removed:
            return LinkReply(_nothing_to_remove(name, target))
        return LinkReply(_removed_other(name, target) if told else _removed_untold(name, target))

    async def _unlink(self, user_id: str) -> LinkReply:
        current = await self._store.get_identity(user_id)
        if current is None or not await self._store.unlink_identity(user_id):
            return LinkReply(NOTHING_LINKED)
        log.info("user %s unlinked their GitHub login", user_id)
        return LinkReply(_unlinked(current.github_login))
