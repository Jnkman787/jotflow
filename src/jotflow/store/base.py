"""The store's interface, its errors, and the rules both implementations share.

JotFlow stores pointers and derived state, never what people wrote in Slack
(Data and privacy in docs/architecture.md). The store is reached only through
:class:`Store`, so the SQLite implementation can later sit beside a Postgres
one, and tests use :class:`~jotflow.store.memory.MemoryStore`.

Rules every implementation follows, checked by one contract test suite:

- Proposal state changes follow :data:`ALLOWED_TRANSITIONS`, the diagram in
  docs/architecture.md, and are atomic: a change from an unexpected state
  raises :class:`TransitionError` and changes nothing.
- Approval locks the draft's content hash (D7). Applying needs a result whose
  issue URL matches the proposal.
- Topic titles are cleaned by :func:`~jotflow.store.titles.clean_title`.
- A person's decision about a message is never overwritten by the agent.
- A topic and the messages linked to it are always in the same channel.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    MessageRef,
    ProposalRecord,
    ProposalState,
    Topic,
)
from jotflow.store.hashing import content_hash

LinkSource = Literal["agent", "person"]
Clock = Callable[[], datetime]

S = ProposalState
ALLOWED_TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    S.PENDING: frozenset({S.APPROVED, S.REJECTED, S.SUPERSEDED}),
    S.APPROVED: frozenset({S.EXECUTING}),
    S.EXECUTING: frozenset({S.APPLIED, S.RECONCILING, S.FAILED}),
    S.RECONCILING: frozenset({S.APPLIED, S.APPROVED, S.FAILED}),
    S.FAILED: frozenset({S.APPROVED}),
    S.APPLIED: frozenset(),
    S.REJECTED: frozenset(),
    S.SUPERSEDED: frozenset(),
}

AUTO_APPROVER = "auto"

_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


class StoreError(Exception):
    """Base class for store errors."""


class NotFoundError(StoreError):
    """The record does not exist."""


class ConflictError(StoreError):
    """The write would break a uniqueness or consistency rule."""


class TransitionError(StoreError):
    """The proposal is not in a state that allows the requested change."""

    def __init__(self, proposal_id: str, current: ProposalState, wanted: ProposalState) -> None:
        super().__init__(f"proposal {proposal_id} is {current}, so it cannot become {wanted}")
        self.proposal_id = proposal_id
        self.current = current
        self.wanted = wanted


class ContentChangedError(StoreError):
    """Approval named a content hash that no longer matches the draft."""


@dataclass(frozen=True)
class MessageLink:
    """What the store knows about one Slack message: where it belongs, and who decided."""

    ref: MessageRef
    topic_id: str | None
    chatter: bool
    linked_by: LinkSource
    linked_at: datetime


@dataclass(frozen=True)
class TopicRecord:
    """A topic with its activity times. ``topic.messages`` holds the linked pointers."""

    topic: Topic
    created_at: datetime
    last_activity_at: datetime


@dataclass(frozen=True)
class IdentityLink:
    slack_user_id: str
    github_login: str
    verified: bool
    linked_at: datetime


@dataclass(frozen=True)
class ChannelModeSetting:
    channel_id: str
    mode: ChannelMode
    changed_by: str
    changed_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_text(moment: datetime) -> str:
    """Store a time as UTC ISO 8601. Naive times are refused."""
    if moment.tzinfo is None:
        raise ValueError("times must carry a timezone")
    # Fixed width, so text order in SQL is time order.
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def from_text(text: str) -> datetime:
    return datetime.fromisoformat(text)


def check_github_login(login: str) -> str:
    if not _GITHUB_LOGIN.fullmatch(login):
        raise ValueError(f"{login[:40]!r} is not a GitHub login")
    return login


def check_id(value: str, what: str) -> str:
    if not value or not value.strip() or len(value) > 200:
        raise ValueError(f"{what} must be a non-empty id of at most 200 characters")
    return value


def check_new_proposal(record: ProposalRecord) -> None:
    """A proposal enters the store pending, unapproved, and without a result."""
    check_id(record.id, "proposal id")
    check_id(record.channel_id, "channel id")
    check_id(record.run_id, "run id")
    if record.state is not ProposalState.PENDING:
        raise ValueError("a new proposal must be pending")
    if record.content_hash or record.approved_by or record.approved_at or record.result:
        raise ValueError("a new proposal cannot be approved or carry a result yet")
    for user in record.participants:
        check_id(user, "participant")


@dataclass(frozen=True)
class TransitionChanges:
    """The columns a transition writes, worked out before anything is written."""

    state: ProposalState
    content_hash: str | None
    approved_by: str | None
    approved_at: datetime | None
    result: ExecutionResult | None


def plan_transition(
    record: ProposalRecord,
    to: ProposalState,
    *,
    expected: Collection[ProposalState] | None,
    approved_by: str | None,
    expected_hash: str | None,
    result: ExecutionResult | None,
    now: datetime,
) -> TransitionChanges:
    """Check a requested transition against the record and return what to write.

    Raises :class:`TransitionError` for a disallowed or unexpected state,
    :class:`ContentChangedError` when ``expected_hash`` is stale, and
    ``ValueError`` for missing or misplaced arguments. Writes nothing.
    """
    current = record.state
    if to not in ALLOWED_TRANSITIONS[current] or (expected is not None and current not in expected):
        raise TransitionError(record.id, current, to)
    if (approved_by is not None or expected_hash is not None) and to is not ProposalState.APPROVED:
        raise ValueError("approved_by and expected_hash only apply when approving")
    if result is not None and to not in (ProposalState.APPLIED, ProposalState.FAILED):
        raise ValueError("a result only applies when a proposal is applied or fails")

    changes = TransitionChanges(
        state=to,
        content_hash=record.content_hash,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
        result=record.result,
    )
    if to is ProposalState.APPROVED:
        if current is ProposalState.RECONCILING:
            # A retry after an interrupted run keeps the original approval.
            return changes
        if not approved_by:
            raise ValueError("approving needs approved_by: a Slack user id or 'auto'")
        check_id(approved_by, "approver")
        locked = content_hash(record.proposal)
        if expected_hash is not None and expected_hash != locked:
            raise ContentChangedError(
                f"proposal {record.id} changed after its card was shown; show it again"
            )
        return TransitionChanges(to, locked, approved_by, now, record.result)
    if to is ProposalState.APPLIED:
        if result is None or not result.matches(record.proposal):
            raise ValueError("applying needs a result whose issue URL matches the proposal")
        return TransitionChanges(
            to, record.content_hash, record.approved_by, record.approved_at, result
        )
    if to is ProposalState.FAILED and result is not None:
        if result.error is None:
            raise ValueError("a failed proposal's result must be an error")
        return TransitionChanges(
            to, record.content_hash, record.approved_by, record.approved_at, result
        )
    return changes


class Store(Protocol):
    """JotFlow's local state. Every implementation passes tests/test_store_contract.py."""

    async def close(self) -> None: ...

    # Slack message pointers
    async def link_message(
        self, ref: MessageRef, topic_id: str, *, by: LinkSource = "agent"
    ) -> MessageLink:
        """Link a message to a topic in the same channel, refreshing the topic's activity.

        If a person already decided where this message belongs and ``by`` is
        ``agent``, nothing changes and the person's decision is returned.
        """
        ...

    async def mark_chatter(self, ref: MessageRef, *, by: LinkSource = "agent") -> MessageLink:
        """Record that a message is not an idea, with the same override rule."""
        ...

    async def get_message(self, channel_id: str, ts: str) -> MessageLink | None: ...

    # Topics
    async def create_topic(self, channel_id: str, title: str) -> TopicRecord:
        """A new topic with a random id and a cleaned title."""
        ...

    async def get_topic(self, topic_id: str) -> TopicRecord | None: ...

    async def rename_topic(self, topic_id: str, title: str) -> TopicRecord: ...

    async def set_topic_issue(self, topic_id: str, repo: str, issue_number: int) -> TopicRecord: ...

    async def channel_topics(
        self, channel_id: str, *, active_since: datetime | None = None
    ) -> list[TopicRecord]:
        """The channel's topics, most recently active first."""
        ...

    # Proposals
    async def add_proposal(self, record: ProposalRecord) -> ProposalRecord: ...

    async def get_proposal(self, proposal_id: str) -> ProposalRecord | None: ...

    async def thread_proposals(
        self, channel_id: str, thread_ts: str | None
    ) -> list[ProposalRecord]:
        """Proposals from one thread, oldest first."""
        ...

    async def transition(
        self,
        proposal_id: str,
        to: ProposalState,
        *,
        expected: Collection[ProposalState] | None = None,
        approved_by: str | None = None,
        expected_hash: str | None = None,
        result: ExecutionResult | None = None,
    ) -> ProposalRecord:
        """Move a proposal to ``to`` atomically; see :func:`plan_transition`."""
        ...

    async def supersede_pending(
        self, channel_id: str, thread_ts: str | None, *, keep_id: str
    ) -> list[str]:
        """Mark every other pending proposal in the thread superseded; return their ids."""
        ...

    # Identity links
    async def link_identity(self, slack_user_id: str, github_login: str) -> IdentityLink:
        """Self-declared in v1, so stored unverified. A login is claimed by one person."""
        ...

    async def get_identity(self, slack_user_id: str) -> IdentityLink | None: ...

    async def unlink_identity(self, slack_user_id: str) -> bool: ...

    # Channel modes
    async def set_channel_mode(
        self, channel_id: str, mode: ChannelMode, *, changed_by: str
    ) -> ChannelModeSetting: ...

    async def get_channel_mode(self, channel_id: str) -> ChannelModeSetting | None: ...
