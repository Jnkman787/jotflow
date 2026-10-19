"""Keep every proposal's card in Slack in step with the store (#152).

The store is the card's outbox. Every change a card shows raises the
proposal's revision, and the card records which revision it shows. One loop,
:meth:`CardSync.sync`, asks the store for the cards that are missing or behind,
posts or edits each one, and records what it drew. So nothing that writes a
proposal calls Slack. ``propose_issue`` stays a pure store write that a Slack
outage cannot fail, and a card that could not be drawn stays due and is tried
again, across restarts too.

A proposal superseded or rejected before its card went up never gets one.
Every other state is drawn: a pending card, an auto-approved one (#28), and
each later state edited in place.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from jotflow.models import ProposalRecord, ProposalState
from jotflow.orchestrator.cards import CardContext, CardPublisher
from jotflow.policy import Policy, UnknownChannelError
from jotflow.store.base import AUTO_APPROVER, IdentityLink, Store, utc_now

log = logging.getLogger("jotflow.cards")

SYNC_SECONDS = 1.0
BATCH = 50
FIRST_RETRY = timedelta(seconds=2)
LAST_RETRY = timedelta(minutes=1)

# A proposal that ended before anyone saw it needs no card.
_NO_CARD_NEEDED = frozenset({ProposalState.SUPERSEDED, ProposalState.REJECTED})
# Auto mode posts its summary card after the write (#28): not while it is under way.
_WRITING = frozenset({ProposalState.APPROVED, ProposalState.EXECUTING, ProposalState.RECONCILING})
AUTO = AUTO_APPROVER

# Slack errors that retrying cannot fix: the card's message or its channel is gone.
FINAL_SLACK_ERRORS = frozenset(
    {
        "message_not_found",
        "channel_not_found",
        "is_archived",
        "not_in_channel",
        "cant_update_message",
    }
)


def _slack_error(exc: Exception) -> str | None:
    """The error code of a Slack API error, such as ``message_not_found``."""
    response = getattr(exc, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except Exception:
        return None
    return code if isinstance(code, str) else None


class People(Protocol):
    """Display names for Slack users. Tests use a fake."""

    async def name(self, user_id: str) -> str | None: ...


async def card_context(
    policy: Policy, store: Store, people: People, record: ProposalRecord
) -> CardContext:
    """Everything the card shows besides the proposal: names, identities, the channel."""
    users = list(dict.fromkeys(record.participants))
    if record.approved_by and record.approved_by != AUTO:
        users.append(record.approved_by)
    names: dict[str, str] = {}
    identities: dict[str, IdentityLink] = {}
    for user in users:
        if (name := await people.name(user)) is not None:
            names[user] = name
        if (link := await store.get_identity(user)) is not None:
            identities[user] = link
    return CardContext(
        channel=policy.channel(record.channel_id), names=names, identities=identities
    )


class CardSync:
    """Posts each due card, or edits it in place, and records what it drew."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        publisher: CardPublisher,
        people: People,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._policy = policy
        self._publisher = publisher
        self._people = people
        self._clock = clock
        # proposal id -> (when to try next, how long the last wait was)
        self._waiting: dict[str, tuple[datetime, timedelta]] = {}
        # One draw at a time, so sync and refresh never both post the same new card.
        self._lock = asyncio.Lock()

    async def sync(self) -> int:
        """Draw every due card that is not waiting to retry. Returns how many were drawn."""
        drawn = 0
        now = self._clock()
        # Ask for enough that proposals waiting to retry cannot crowd out the rest.
        for record in await self._store.cards_due(BATCH + len(self._waiting)):
            wait = self._waiting.get(record.id)
            if wait is not None and wait[0] > now:
                continue
            if await self._draw(record):
                drawn += 1
        return drawn

    async def refresh(self, proposal_id: str) -> bool:
        """Draw one proposal's card now, if it is due. For callers that just changed it."""
        record = await self._store.get_proposal(proposal_id)
        if record is None:
            return False
        return await self._draw(record)

    async def _draw(self, seen: ProposalRecord) -> bool:
        async with self._lock:
            # Read it again under the lock: another draw may have just posted it.
            record = await self._store.get_proposal(seen.id)
            if record is None or not record.card_due:
                return False
            return await self._draw_now(record)

    async def _draw_now(self, record: ProposalRecord) -> bool:
        try:
            if record.card_ts is None and record.state in _NO_CARD_NEEDED:
                await self._store.mark_card_shown(record.id, record.revision)
                return False
            if record.card_ts is None and record.approved_by == AUTO and record.state in _WRITING:
                return False  # stays due: posted once the write is applied or failed
            context = await card_context(self._policy, self._store, self._people, record)
            await self._publisher.show(record, context)
            await self._store.mark_card_shown(record.id, record.revision)
        except UnknownChannelError:
            # jotflow.toml no longer lists the channel: there is nowhere to draw it.
            log.warning("proposal %s: its channel is not configured; no card", record.id)
            await self._store.mark_card_shown(record.id, record.revision)
            return False
        except Exception as exc:
            if (code := _slack_error(exc)) in FINAL_SLACK_ERRORS:
                # Someone deleted the card, or the channel is gone: never post it again.
                log.warning(
                    "proposal %s: Slack said %s; the card is left as it is", record.id, code
                )
                self._waiting.pop(record.id, None)
                await self._store.mark_card_shown(record.id, record.revision)
                return False
            self._back_off(record.id)
            # The type only: an error's text could quote the card.
            log.warning("proposal %s: could not draw its card (%s)", record.id, type(exc).__name__)
            return False
        self._waiting.pop(record.id, None)
        log.info("proposal %s: card shows revision %d", record.id, record.revision)
        return True

    def _back_off(self, proposal_id: str) -> None:
        last = self._waiting.get(proposal_id)
        wait = FIRST_RETRY if last is None else min(last[1] * 2, LAST_RETRY)
        self._waiting[proposal_id] = (self._clock() + wait, wait)


class BoltPeople:
    """:class:`People` over slack-sdk's client, cached for the life of the process."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._names: dict[str, str | None] = {}

    async def name(self, user_id: str) -> str | None:
        if user_id not in self._names:
            try:
                response = await self._client.users_info(user=user_id)
            except Exception as exc:
                log.warning("could not look up a Slack user (%s)", type(exc).__name__)
                return None
            self._names[user_id] = _display_name(response.get("user") or {})
        return self._names[user_id]


def _display_name(user: Mapping[str, Any]) -> str | None:
    profile = user.get("profile") or {}
    for value in (profile.get("display_name"), profile.get("real_name"), user.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
