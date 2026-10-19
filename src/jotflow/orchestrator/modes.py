"""Channel modes: ``propose``, ``auto``, and ``paused`` (#28, D2, D7).

The mode is runtime state in the store, changed with ``/jotflow mode``.

- ``propose``, the default: a card with Approve, and nothing is written until a
  person approves.
- ``auto``: :class:`AutoApprover` approves each new proposal as ``auto`` and
  queues its execution run. The card is posted once the write has finished.
- ``paused``: nothing is read and nothing is written. The store holds runs and
  cards in a paused channel, and the orchestrator ignores its messages.

Who may change the mode follows D7: any workspace member while approvers are
off, only an approver while they are on, and never a guest or an external
user. Every change is announced in the channel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from jotflow.models import ChannelMode, ProposalState, RunKind
from jotflow.orchestrator.card_sync import AUTO
from jotflow.orchestrator.packet import Budget, Sleep, with_backoff
from jotflow.orchestrator.people import Person, classify
from jotflow.policy import Policy
from jotflow.store.base import Store, TransitionError
from jotflow.store.hashing import content_hash

log = logging.getLogger("jotflow.modes")

ModeFor = Callable[[str], Awaitable[ChannelMode]]

# The most members checked for guests before warning anyway.
MAX_MEMBERS_CHECKED = 1000
# The guest scan reads the workspace's directory with users.list (#161): one
# pass, stopping once every member is found, at most this many pages.
USERS_PAGE_SIZE = 500
MAX_USER_PAGES = 40

MEANING = {
    ChannelMode.PROPOSE: (
        "JotFlow posts each proposal as a card, and nothing is written to GitHub until a "
        "person approves it."
    ),
    ChannelMode.AUTO: (
        "JotFlow writes each proposal to GitHub straight away, without approval, and then "
        "posts it as a card."
    ),
    ChannelMode.PAUSED: (
        "JotFlow reads nothing and writes nothing here until someone sets another mode."
    ),
}
AUTO_WARNING = (
    "Warning: this channel has {who}. In auto mode, text from anyone here reaches GitHub "
    "with no human review. Consider turning approvers on in jotflow.toml."
)
USAGE = "Usage: /jotflow mode, or /jotflow mode propose, auto, or paused."
NOT_SET_UP = "This channel isn't set up for JotFlow, so it has no mode."
NO_GUESTS = "Guests and people from other organisations can't change JotFlow's mode."
APPROVERS_ONLY = "Only this channel's approvers can change JotFlow's mode."
ALREADY = "JotFlow is already in {mode} mode here."


class AutoApprover:
    """Approves new proposals in ``auto`` channels and queues their writes (#28).

    Only a proposal whose card has not been posted: switching a channel to
    ``auto`` never approves something a person was already looking at.
    """

    def __init__(self, store: Store, mode_for: ModeFor) -> None:
        self._store = store
        self._mode_for = mode_for

    async def approve_due(self) -> int:
        approved = 0
        for record in await self._store.cards_due():
            if record.state is not ProposalState.PENDING or record.card_ts is not None:
                continue
            if await self._mode_for(record.channel_id) is not ChannelMode.AUTO:
                continue
            try:
                done = await self._store.transition(
                    record.id,
                    ProposalState.APPROVED,
                    expected={ProposalState.PENDING},
                    approved_by=AUTO,
                    expected_hash=content_hash(record.proposal),
                )
            except TransitionError:
                continue  # superseded or changed meanwhile: nothing to approve
            await self._store.enqueue_run(
                RunKind.EXECUTION, done.channel_id, done.thread_ts, proposal_id=done.id
            )
            log.info("proposal %s approved by auto mode; execution queued", done.id)
            approved += 1
        return approved


class ModeSlack(Protocol):
    """The Slack calls the mode command makes. Tests use a fake."""

    async def person(self, user_id: str) -> Person: ...

    async def name(self, user_id: str) -> str | None: ...

    async def announce(self, channel_id: str, text: str) -> None: ...

    async def outsiders(self, channel_id: str) -> str | None:
        """Who in the channel is a guest or external, in words, or None if nobody is."""
        ...


@dataclass(frozen=True)
class ModeReply:
    """What the person who typed the command sees, privately."""

    text: str
    changed: bool = False


class ModeCommand:
    """``/jotflow mode``: show the mode, or set it and announce the change."""

    def __init__(self, store: Store, policy: Policy, slack: ModeSlack) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack

    async def handle(self, channel_id: str, user_id: str, argument: str) -> ModeReply:
        if channel_id not in self._policy.channels:
            return ModeReply(NOT_SET_UP)
        setting = await self._store.get_channel_mode(channel_id)
        current = ChannelMode.PROPOSE if setting is None else setting.mode
        wanted = argument.strip().lower()
        if not wanted:
            return ModeReply(await self._describe(current, setting))
        try:
            mode = ChannelMode(wanted)
        except ValueError:
            return ModeReply(USAGE)
        person = await self._slack.person(user_id)
        if person.guest or person.external:
            return ModeReply(NO_GUESTS)
        approval = self._policy.approval
        if approval.approvers_enabled and user_id not in approval.approvers:
            return ModeReply(APPROVERS_ONLY)
        if mode is current:
            return ModeReply(ALREADY.format(mode=mode.value))
        await self._store.set_channel_mode(channel_id, mode, changed_by=user_id)
        log.info("channel %s set to %s mode by %s", channel_id, mode.value, user_id)
        name = await self._slack.name(user_id) or "Someone"
        lines = [f"{name} set JotFlow to {mode.value} mode in this channel.", MEANING[mode]]
        if mode is ChannelMode.AUTO:
            try:
                who = await self._slack.outsiders(channel_id)
            except Exception as exc:
                # Fail closed: an unchecked channel gets the warning.
                log.warning("could not check %s's members (%s)", channel_id, type(exc).__name__)
                who = "members JotFlow could not check"
            if who is not None:
                lines.append(AUTO_WARNING.format(who=who))
        await self._slack.announce(channel_id, "\n".join(lines))
        return ModeReply(f"Done: JotFlow is in {mode.value} mode here.", changed=True)

    async def _describe(self, mode: ChannelMode, setting: Any) -> str:
        lines = [f"JotFlow is in {mode.value} mode here. {MEANING[mode]}"]
        if setting is not None:
            name = await self._slack.name(setting.changed_by) or "someone"
            when = setting.changed_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            lines.append(f"Set by {name}, {when}.")
        lines.append(USAGE)
        return "\n".join(lines)


def _plain_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "section", "text": {"type": "plain_text", "text": text, "emoji": False}}]


class BoltModeSlack:
    """:class:`ModeSlack` over slack-sdk's client, with the shared people helper."""

    def __init__(
        self, client: Any, people: Any, names: Any, *, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._client = client
        self._people = people
        self._names = names
        self._sleep = sleep

    async def person(self, user_id: str) -> Person:
        result: Person = await self._people.person(user_id)
        return result

    async def name(self, user_id: str) -> str | None:
        result: str | None = await self._names.name(user_id)
        return result

    async def announce(self, channel_id: str, text: str) -> None:
        await self._client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=_plain_blocks(text),
            unfurl_links=False,
            unfurl_media=False,
        )

    async def outsiders(self, channel_id: str) -> str | None:
        """What a channel holds besides workspace members, or None. Fails closed.

        Reads the channel's members, then pages ``users.list`` once (#161),
        instead of ``users.info`` per member. A member the directory doesn't
        list, such as someone from another organisation, counts as a guest.
        Rate limits are retried within one budget. A listing that still fails
        raises, and the caller warns.
        """
        budget = Budget()

        async def call(what: str, method: Callable[[], Awaitable[Any]]) -> Any:
            return await with_backoff(method, what=what, sleep=self._sleep, budget=budget)

        info = await call(
            "the channel's details", lambda: self._client.conversations_info(channel=channel_id)
        )
        channel: Mapping[str, Any] = info.get("channel") or {}
        shared = bool(channel.get("is_ext_shared") or channel.get("is_shared"))
        members, complete = await self._members(channel_id, call)
        unknown = set(members)
        guests = 0
        own = await self._own_team()
        cursor: str | None = None
        for _ in range(MAX_USER_PAGES):
            if not unknown:
                break
            page = await call(
                "the workspace's people",
                partial(self._client.users_list, limit=USERS_PAGE_SIZE, cursor=cursor),
            )
            for user in page.get("members") or []:
                user_id = user.get("id") if isinstance(user, Mapping) else None
                if user_id in unknown:
                    unknown.discard(user_id)
                    person = classify(user_id, user, own)
                    guests += person.guest or person.external
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        else:
            if unknown and cursor:
                complete = False  # the directory is larger than one scan reads
        if complete:
            guests += len(unknown)  # not in this workspace's directory: fail closed
        parts = []
        if shared:
            parts.append("members from another organisation")
        if guests:
            parts.append("guests" if guests > 1 else "a guest")
        if not complete:
            parts.append("more members than JotFlow could check")
        return " and ".join(parts) if parts else None

    async def _members(
        self, channel_id: str, call: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]]
    ) -> tuple[list[str], bool]:
        """Up to :data:`MAX_MEMBERS_CHECKED` member ids, and whether that was all."""
        members: list[str] = []
        cursor: str | None = None
        while True:
            page = await call(
                "the channel's members",
                partial(
                    self._client.conversations_members, channel=channel_id, limit=200, cursor=cursor
                ),
            )
            for member in page.get("members") or []:
                if len(members) >= MAX_MEMBERS_CHECKED:
                    return members, False
                if isinstance(member, str):
                    members.append(member)
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return members, True

    async def _own_team(self) -> str | None:
        own_team = getattr(self._people, "own_team", None)
        if own_team is None:
            return None
        team: str | None = await own_team()
        return team
