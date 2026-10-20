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
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from jotflow.models import (
    MAX_RUN_SECONDS,
    ChannelMode,
    Duplicate,
    ExecutionResult,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    RelatedIssue,
    Run,
    RunKind,
    RunStatus,
    Topic,
    UpdateProposal,
)
from jotflow.store.hashing import content_hash

# A run must not outlive its MCP token; both caps are models.MAX_RUN_SECONDS.
MAX_RUN_TIMEOUT_SECONDS = MAX_RUN_SECONDS

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

# Once final, a proposal's drafted text is cleared (#102). Failed is not final:
# a person may retry it, until the draft is cleared explicitly.
FINAL_STATES = frozenset({S.APPLIED, S.REJECTED, S.SUPERSEDED})
CLEARABLE_STATES = FINAL_STATES | {S.FAILED}
CLEARED_TITLE = "(cleared)"

R = RunStatus
# Run lifecycle; see the run diagram in docs/architecture.md. Interrupted is
# set only by recover_runs at startup, so it is not a finish_run target.
ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    R.QUEUED: frozenset({R.RUNNING, R.FAILED, R.INTERRUPTED}),
    R.RUNNING: frozenset({R.SUCCEEDED, R.FAILED, R.TIMED_OUT, R.INTERRUPTED}),
    R.SUCCEEDED: frozenset(),
    R.FAILED: frozenset(),
    R.TIMED_OUT: frozenset(),
    R.INTERRUPTED: frozenset(),
}
ACTIVE_RUN_STATUSES = frozenset({R.QUEUED, R.RUNNING})
FINISH_STATUSES = frozenset({R.SUCCEEDED, R.FAILED, R.TIMED_OUT})
MAX_RUN_ERROR = 2000

DEFAULT_RUN_TIMEOUTS: dict[RunKind, timedelta] = {
    RunKind.DRAFTING: timedelta(minutes=10),
    RunKind.EXECUTION: timedelta(minutes=5),
    RunKind.QUESTION: timedelta(minutes=5),
}

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


class DraftClearedError(TransitionError):
    """The proposal's draft has been cleared, so it can no longer be approved or executed."""

    def __init__(self, proposal_id: str, current: ProposalState, wanted: ProposalState) -> None:
        super().__init__(proposal_id, current, wanted)
        self.args = (f"proposal {proposal_id}'s draft was cleared, so it cannot become {wanted}",)


class ContentChangedError(StoreError):
    """Approval named a content hash that no longer matches the draft."""


class DuplicateRefusedError(ConflictError):
    """A "same as" or its undo can't apply; ``str()`` is the fixed reason (#36)."""


# The fixed reasons a person sees. None quotes the draft, the issue, or the person.
DUPLICATE_NOT_PENDING = "This proposal is no longer waiting, so it can't be marked the same."
DUPLICATE_NOT_NEW = "Only a proposed new issue can be marked the same as an existing one."
DUPLICATE_NO_CANDIDATE = "That related issue isn't on this proposal any more."
DUPLICATE_TOPIC_LINKED = (
    "This conversation is already linked to an issue, so it can't be marked the same as another."
)
DUPLICATE_NOT_MARKED = "This proposal wasn't marked the same as an existing issue."
DUPLICATE_UNDONE = "That was already undone."
DUPLICATE_MOVED_ON = (
    "This conversation has moved on since: it points at another issue, or something was "
    "written to the issue after it was marked. Nothing changed."
)


# A proposal in one of these states relies on its topic's issue binding (#36).
BOUND_STATES = frozenset(
    {
        ProposalState.PENDING,
        ProposalState.APPROVED,
        ProposalState.EXECUTING,
        ProposalState.RECONCILING,
        ProposalState.APPLIED,
    }
)


def plan_duplicate(record: ProposalRecord, index: int, expected_hash: str) -> RelatedIssue:
    """The related issue a "same as" binds to, from the *stored* draft (#36).

    Never from the click's own value: the click names only the index and the
    hash of the draft the card showed, so a stale or forged click is refused.
    """
    if record.state is not ProposalState.PENDING:
        raise DuplicateRefusedError(DUPLICATE_NOT_PENDING)
    draft = record.proposal
    if not isinstance(draft, IssueProposal) or draft.topic_id is None:
        raise DuplicateRefusedError(DUPLICATE_NOT_NEW)
    if record.draft_cleared_at is not None:
        raise DraftClearedError(record.id, record.state, ProposalState.REJECTED)
    if content_hash(draft) != expected_hash:
        raise ContentChangedError(f"proposal {record.id} changed since the card was drawn")
    if not 0 <= index < len(draft.related):
        raise DuplicateRefusedError(DUPLICATE_NO_CANDIDATE)
    return draft.related[index]


def plan_undo(record: ProposalRecord) -> Duplicate:
    """The "same as" an undo takes back. The store also checks the topic hasn't moved on."""
    if record.state is not ProposalState.REJECTED or record.duplicate is None:
        raise DuplicateRefusedError(DUPLICATE_NOT_MARKED)
    if record.duplicate.undone_at is not None:
        raise DuplicateRefusedError(DUPLICATE_UNDONE)
    return record.duplicate


@dataclass(frozen=True)
class Recovery:
    """What :meth:`Store.recover_runs` did at startup, for the log."""

    interrupted: tuple[str, ...]  # run ids marked interrupted
    requeued: tuple[str, ...]  # new run ids queued to replace them
    reconciling: tuple[str, ...]  # proposal ids moved from executing to reconciling


def check_run_request(
    kind: RunKind, channel_id: str, thread_ts: str, proposal_id: str | None, timeout: timedelta
) -> int:
    """Validate an enqueue request; return the timeout in whole seconds."""
    check_id(channel_id, "channel id")
    # A top-level post is its own thread: pass its ts, never None, so a reply
    # to it, whose thread_ts is that same ts, shares the per-thread lock.
    check_id(thread_ts, "thread ts")
    if kind is RunKind.EXECUTION:
        if proposal_id is None:
            raise ValueError("an execution run needs proposal_id")
        check_id(proposal_id, "proposal id")
    elif proposal_id is not None:
        raise ValueError("only an execution run names a proposal")
    seconds = int(timeout.total_seconds())
    if not 0 < seconds <= MAX_RUN_TIMEOUT_SECONDS:
        raise ValueError(
            f"a run timeout must be between 1 second and {MAX_RUN_TIMEOUT_SECONDS // 3600} hours, "
            "the longest an MCP token lasts"
        )
    return seconds


def check_finish(run: Run, status: RunStatus, error: str | None) -> str | None:
    """Validate a finish request; return the bounded error to store.

    Raises ``ValueError`` for a status that is not a finish, or an error on a
    success. A run that already finished is not an error here; callers check
    :data:`ACTIVE_RUN_STATUSES` and return None, so a late timeout and a late
    success cannot both win.
    """
    if status not in FINISH_STATUSES:
        raise ValueError(f"{status} is not a finishing status")
    if status is RunStatus.SUCCEEDED and error is not None:
        raise ValueError("a successful run has no error")
    if run.status is RunStatus.QUEUED and status is not RunStatus.FAILED:
        raise ValueError("a queued run can only be cancelled, as failed")
    return None if error is None else error[:MAX_RUN_ERROR]


@dataclass(frozen=True)
class MessageLink:
    """What the store knows about one Slack message: where it belongs, and who decided."""

    ref: MessageRef
    topic_id: str | None
    chatter: bool
    linked_by: LinkSource
    linked_at: datetime


@dataclass(frozen=True)
class PendingThread:
    """A thread waiting out its quiet window (#130). Pointers and a time only."""

    channel_id: str
    thread_ts: str
    due_at: datetime


@dataclass(frozen=True)
class TopicRecord:
    """A topic with its activity times. ``topic.messages`` holds the linked pointers."""

    topic: Topic
    created_at: datetime
    last_activity_at: datetime


@dataclass(frozen=True)
class OperatorToken:
    """An admin-issued, read-only token for a person's terminal agent (#58).

    Only the digest is held; the token is shown once, when it is created.
    """

    id: str
    name: str
    digest: str = field(repr=False)
    channels: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    def usable(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at


@dataclass(frozen=True)
class IdentityLink:
    slack_user_id: str
    github_login: str
    verified: bool
    linked_at: datetime
    # Set once the person proved control of the account (#195): when, and GitHub's
    # numeric user id, which survives a rename.
    verified_at: datetime | None = None
    github_id: int | None = None


# ``removed_by`` when a person proved control of a login someone else had linked (#195).
VERIFICATION_REMOVER = "verification"


@dataclass(frozen=True)
class IdentityCode:
    """A pending one-time code (#195). Only its digest is kept."""

    slack_user_id: str
    github_login: str
    code_digest: str = field(repr=False)
    expires_at: datetime
    checks: int = 0


@dataclass(frozen=True)
class Verified:
    """What finishing a verification did: the new link, and whose link it replaced."""

    link: IdentityLink
    displaced: str | None = None


# ``removed_by`` for a removal made with the host CLI rather than in Slack (#173).
OPERATOR_REMOVER = "operator"


@dataclass(frozen=True)
class IdentityUnlink:
    """Someone removed another person's link (#173). Who, whom and when; no login or text."""

    id: str
    slack_user_id: str
    removed_by: str
    removed_at: datetime


@dataclass(frozen=True)
class ChannelModeSetting:
    channel_id: str
    mode: ChannelMode
    changed_by: str
    changed_at: datetime


def cleared_draft(
    proposal: IssueProposal | UpdateProposal,
) -> IssueProposal | UpdateProposal:
    """The draft with its free text removed: title, body, comment, and the agent's reason
    for overruling a hint, which can quote people too (#147). All else stays."""
    if isinstance(proposal, IssueProposal):
        update = {"title": CLEARED_TITLE, "body": "", "hint_overruled": None}
        return proposal.model_copy(update=update, deep=True)
    changes = proposal.changes.model_copy(update={"body": None, "comment": None})
    return proposal.model_copy(update={"changes": changes, "hint_overruled": None}, deep=True)


def check_clearable(record: ProposalRecord) -> None:
    """Only a final or failed proposal may lose its draft; others still need it."""
    if record.state not in CLEARABLE_STATES:
        error = TransitionError(record.id, record.state, record.state)
        error.args = (f"proposal {record.id} is {record.state}, so its draft is still needed",)
        raise error


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


NoticeKind = Literal["guide", "tip", "nudge"]
# Keywords a reaction can stand for (#138, #139).
ReactionKeyword = Literal["idea", "bug", "skip"]
REACTION_KEYWORDS: tuple[ReactionKeyword, ...] = ("idea", "bug", "skip")
# The mark a message's own ``skip:`` text leaves, so removing a reaction never undoes it.
TEXT_MARK = "text"


@dataclass(frozen=True)
class ReactionMark:
    """One person's reaction standing for a keyword on one message."""

    channel_id: str
    ts: str
    user_id: str
    keyword: ReactionKeyword
    marked_at: datetime


def check_mark(channel_id: str, ts: str, user_id: str, keyword: str) -> None:
    check_id(channel_id, "channel id")
    check_id(ts, "message timestamp")
    check_id(user_id, "Slack user id")
    if keyword not in REACTION_KEYWORDS:
        raise ValueError(f"no reaction stands for {keyword!r}")


# What a person changed from the card (#160). The value itself is never stored.
EditField = Literal["repo", "assign", "unassign"]
EDIT_FIELDS: tuple[EditField, ...] = ("repo", "assign", "unassign")
# A person edits only what is still waiting for approval, or a failed write to retry.
EDITABLE_STATES = frozenset({ProposalState.PENDING, ProposalState.FAILED})


@dataclass(frozen=True)
class ProposalEdit:
    """One edit made from the card: who, when, and which field, never the value."""

    proposal_id: str
    edited_by: str
    field: EditField
    edited_at: datetime


def check_edit(
    record: ProposalRecord, draft: IssueProposal | UpdateProposal, by: str, field: str
) -> None:
    """Raise unless ``draft`` may replace ``record``'s draft as an edit by ``by``."""
    check_id(by, "Slack user id")
    if field not in EDIT_FIELDS:
        raise ValueError(f"unknown edit {field!r}")
    if record.state not in EDITABLE_STATES or record.draft_cleared_at is not None:
        error = TransitionError(record.id, record.state, record.state)
        error.args = (f"proposal {record.id} is {record.state}, so it can't be edited",)
        raise error
    if draft.kind != record.proposal.kind or draft.topic_id != record.proposal.topic_id:
        raise ValueError("an edit keeps the proposal's kind and topic")


def edited_draft(
    record: ProposalRecord, draft: IssueProposal | UpdateProposal
) -> IssueProposal | UpdateProposal:
    """``draft`` as stored after an edit: a new repo drops the agent's visibility report,
    which was about the old repo (#122), so the card re-derives it from policy."""
    if draft.repo.casefold() != record.proposal.repo.casefold() and draft.repo_visibility:
        return draft.model_copy(update={"repo_visibility": None})
    return draft


def check_notice(channel_id: str, user_id: str, kind: str) -> None:
    check_id(channel_id, "channel id")
    if kind not in ("guide", "tip", "nudge"):
        raise ValueError(f"unknown help notice {kind!r}")
    if (kind == "guide") != (user_id == ""):
        raise ValueError("the guide is for the channel; tips and nudges are for one person")
    if user_id:
        check_id(user_id, "Slack user id")


def check_id(value: str, what: str) -> str:
    if not value or not value.strip() or len(value) > 200:
        raise ValueError(f"{what} must be a non-empty id of at most 200 characters")
    return value


def check_new_proposal(record: ProposalRecord) -> None:
    """A proposal enters the store pending, unapproved, and without a result."""
    check_id(record.id, "proposal id")
    check_id(record.channel_id, "channel id")
    check_id(record.run_id, "run id")
    # A top-level post is its own thread: its ts, never empty, so the proposal
    # shares the per-thread lock, supersede_pending, and recovery with its runs.
    check_id(record.thread_ts, "thread ts")
    if record.state is not ProposalState.PENDING:
        raise ValueError("a new proposal must be pending")
    if record.card_ts is not None or record.card_revision is not None:
        raise ValueError("a new proposal has no card yet")
    if record.revision != 1:
        raise ValueError("a new proposal starts at revision 1")
    if record.thread_told_at is not None:
        raise ValueError("a new proposal has not been applied, so its thread was not told")
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
    clear_draft: bool = False


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

    Moving to a final state also clears the draft (#102). Raises
    :class:`TransitionError` for a disallowed or unexpected state,
    :class:`DraftClearedError` for a cleared draft, :class:`ContentChangedError`
    when ``expected_hash`` is stale, and ``ValueError`` for missing or misplaced
    arguments. Writes nothing.
    """
    changes = _plan(
        record,
        to,
        expected=expected,
        approved_by=approved_by,
        expected_hash=expected_hash,
        result=result,
        now=now,
    )
    if to in FINAL_STATES and record.draft_cleared_at is None:
        return replace(changes, clear_draft=True)
    return changes


def _plan(
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
    if record.draft_cleared_at is not None and to not in FINAL_STATES:
        raise DraftClearedError(record.id, current, to)
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
        if approved_by != AUTO_APPROVER and expected_hash is None:
            # D7: a person approves what their card showed, so the card's hash is required.
            raise ValueError("a person's approval needs expected_hash, the hash the card showed")
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


async def try_transition(
    store: Store,
    proposal_id: str,
    to: ProposalState,
    *,
    expected: Collection[ProposalState] | None = None,
    approved_by: str | None = None,
    expected_hash: str | None = None,
    result: ExecutionResult | None = None,
) -> ProposalRecord | None:
    """Like :meth:`Store.transition`, but return None instead of raising when the
    proposal is not in a state that allows the change, for example because
    another run won the race. Other errors still raise.
    """
    try:
        return await store.transition(
            proposal_id,
            to,
            expected=expected,
            approved_by=approved_by,
            expected_hash=expected_hash,
            result=result,
        )
    except TransitionError:
        return None


@dataclass(frozen=True)
class ToolCallRecord:
    """One write-tool call, as the ledger keeps it. Never holds message text."""

    run_id: str
    tool: str
    args_hash: str
    channel_id: str
    thread_ts: str | None
    result_json: str
    called_at: datetime


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

    async def proposal_for_card(self, channel_id: str, card_ts: str) -> ProposalRecord | None:
        """The proposal whose card is the message ``card_ts`` in ``channel_id``, if any."""
        ...

    async def run_proposals(self, run_id: str) -> list[ProposalRecord]:
        """The proposals ``run_id`` produced, oldest first. Empty for a run that
        drafted nothing, so a finished drafting run with none is recorded as such (#24)."""
        ...

    async def thread_proposals(self, channel_id: str, thread_ts: str) -> list[ProposalRecord]:
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

    async def edit_proposal(
        self,
        proposal_id: str,
        draft: IssueProposal | UpdateProposal,
        *,
        by: str,
        field: EditField,
    ) -> ProposalRecord:
        """Replace a pending or failed proposal's draft with a person's edit (#160).

        Raises the revision, so the card is redrawn, and records who changed
        which field and when, never the value. A failed proposal keeps its old
        approval fields; a retry approves the edited draft's hash.
        """
        ...

    async def proposal_edits(self, proposal_id: str) -> list[ProposalEdit]:
        """The edits made to a proposal from its card, oldest first."""
        ...

    async def set_card_ts(self, proposal_id: str, ts: str) -> ProposalRecord:
        """Record the Slack ts of the proposal's card message, so later states edit it."""
        ...

    async def cards_due(self, limit: int = 50) -> list[ProposalRecord]:
        """Proposals whose card is missing or behind, oldest first (#152).

        Every change a card shows raises a proposal's revision. Recording a
        card's ts does not. Paused channels are left out until unpaused (#28).
        """
        ...

    async def mark_card_shown(self, proposal_id: str, revision: int) -> None:
        """Record that the card now shows ``revision``. Never moves backwards, so a
        change that landed while the card was being drawn keeps it due."""
        ...

    async def threads_due(self, limit: int = 50) -> list[ProposalRecord]:
        """Applied proposals whose thread has not been told the issue URL, oldest first.

        Paused channels are left out until unpaused (#32, #28).
        """
        ...

    async def mark_thread_told(self, proposal_id: str) -> None:
        """Record that the thread was told. Only the first call sets the time."""
        ...

    async def clear_draft(self, proposal_id: str) -> ProposalRecord:
        """Remove a failed or final proposal's drafted text; a no-op if already cleared.

        After this a failed proposal can no longer be retried.
        """
        ...

    async def mark_duplicate(
        self, proposal_id: str, *, index: int, expected_hash: str, by: str
    ) -> ProposalRecord:
        """A person said the proposal is the same as its draft's ``related[index]`` (#36).

        In one step: the proposal becomes ``rejected`` with :attr:`~ProposalRecord.duplicate`
        set, its topic is bound to that issue, and the draft is cleared. Refused, changing
        nothing, unless the proposal is a pending new issue whose draft still hashes to
        ``expected_hash``, and its topic is in the proposal's channel with no issue yet.
        """
        ...

    async def undo_duplicate(self, proposal_id: str, *, by: str) -> ProposalRecord:
        """Take back a "same as": unbind the topic and record who undid it (#36).

        Refused, changing nothing, if the topic now points at another issue, or a
        proposal on the topic drafted after the "same as" is still live or was written
        (:data:`BOUND_STATES`): it relies on the binding.
        """
        ...

    async def supersede_pending(
        self, channel_id: str, thread_ts: str, *, keep_id: str
    ) -> list[str]:
        """Mark every other pending proposal in the thread superseded, clearing their
        drafts; return their ids."""
        ...

    # Identity links
    async def link_identity(self, slack_user_id: str, github_login: str) -> IdentityLink:
        """Self-declared in v1, so stored unverified. A login is claimed by one person."""
        ...

    async def get_identity(self, slack_user_id: str) -> IdentityLink | None: ...

    async def unlink_identity(self, slack_user_id: str) -> bool: ...

    async def remove_identity(self, slack_user_id: str, *, removed_by: str) -> bool:
        """Remove someone else's link and record who did it, in one step (#173).

        Returns False, recording nothing, if they had no link.
        """
        ...

    async def identity_unlinks(self) -> list[IdentityUnlink]:
        """Every removal by someone else, newest first."""
        ...

    async def identity_for_login(self, github_login: str) -> IdentityLink | None:
        """Whoever has linked ``github_login``, compared without case."""
        ...

    # Verification codes (#195)

    async def start_verification(self, code: IdentityCode) -> None:
        """Keep ``code`` as the person's one pending code, replacing any other."""
        ...

    async def get_verification(self, slack_user_id: str) -> IdentityCode | None: ...

    async def count_verification_check(self, slack_user_id: str) -> int:
        """Add one check to the person's pending code, and return the new count."""
        ...

    async def drop_verification(self, slack_user_id: str) -> None: ...

    async def finish_verification(
        self, slack_user_id: str, *, github_login: str, github_id: int
    ) -> Verified:
        """In one step: drop the code, take ``github_login`` from anyone else who
        linked it (recorded as removed by ``verification``), and link it to this
        person, verified now. ``github_login`` is the name as GitHub spells it.
        """
        ...

    # Operator tokens (#58)

    async def add_operator_token(self, token: OperatorToken) -> OperatorToken:
        """Store a new operator token. Raises ConflictError if the name is taken."""
        ...

    async def operator_tokens(self) -> list[OperatorToken]:
        """Every operator token, newest first, revoked and expired ones included."""
        ...

    async def revoke_operator_token(self, name: str) -> OperatorToken | None:
        """Revoke the named token. Returns it, or None if there is no such live token."""
        ...

    async def touch_operator_tokens(self, used: Mapping[str, datetime]) -> None:
        """Record when each token, by id, was last used."""
        ...

    # Channel modes
    async def set_channel_mode(
        self, channel_id: str, mode: ChannelMode, *, changed_by: str
    ) -> ChannelModeSetting: ...

    async def get_channel_mode(self, channel_id: str) -> ChannelModeSetting | None: ...

    # Keyword reactions (#138, #139)
    async def add_reaction_mark(
        self, channel_id: str, ts: str, user_id: str, keyword: ReactionKeyword
    ) -> bool:
        """Record that ``user_id``'s reaction stands for ``keyword`` on the message.

        True only when it is new: the same person reacting twice is one mark.
        """
        ...

    async def remove_reaction_mark(
        self, channel_id: str, ts: str, user_id: str, keyword: ReactionKeyword
    ) -> bool:
        """Remove that mark. True if it was there."""
        ...

    async def reaction_marks(self, channel_id: str, ts: str) -> list[ReactionMark]:
        """The message's reaction marks, oldest first."""
        ...

    async def unmark_chatter(self, ref: MessageRef) -> bool:
        """Remove a person's chatter mark, so the message is unplaced again (#138).

        Leaves an agent's mark, a topic link, or no mark at all alone; True if removed.
        """
        ...

    # Help notices (#80)
    async def claim_notice(self, channel_id: str, user_id: str, kind: NoticeKind) -> bool:
        """Record that ``kind`` goes to ``user_id`` in the channel. True only the first time.

        Claimed just before sending, so two quick messages never both get one.
        The channel's guide uses ``user_id=""``.
        """
        ...

    async def release_notice(self, channel_id: str, user_id: str, kind: NoticeKind) -> None:
        """Undo a claim whose notice could not be sent, so a later message tries again."""
        ...

    # Runs (D8, #53)
    async def enqueue_run(
        self,
        kind: RunKind,
        channel_id: str,
        thread_ts: str,
        *,
        proposal_id: str | None = None,
        timeout: timedelta | None = None,
    ) -> Run:
        """Queue a run, or return the one already queued for the same work.

        A drafting run coalesces with a queued drafting run for the same
        thread; an execution run with a queued or running one for the same
        proposal. ``timeout`` defaults to :data:`DEFAULT_RUN_TIMEOUTS`.

        ``thread_ts`` is the thread's parent ts. For a top-level post with no
        replies that is the post's own ts, as Slack does for its replies.
        """
        ...

    async def claim_next_run(
        self, *, max_running: int, kinds: Collection[RunKind] | None = None
    ) -> Run | None:
        """Start the oldest queued run whose thread has no running run.

        Atomic, and only while fewer than ``max_running`` runs are running. A run
        in a paused channel is never claimed; it waits until the channel is
        unpaused (#28).
        The claimed run's deadline becomes its start plus its timeout. With
        ``kinds``, only runs of those kinds are claimed; the rest stay queued.
        """
        ...

    async def finish_run(
        self, run_id: str, status: RunStatus, *, error: str | None = None
    ) -> Run | None:
        """End a run. Returns None if it had already ended; see :func:`check_finish`."""
        ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def thread_runs(self, channel_id: str, thread_ts: str) -> list[Run]:
        """Runs for one thread, oldest first."""
        ...

    # Threads waiting out their quiet window (#130)

    async def arm_thread(self, channel_id: str, thread_ts: str, due_at: datetime) -> None:
        """Record when the thread falls due, replacing any earlier due time."""
        ...

    async def pending_threads(self) -> list[PendingThread]:
        """Every waiting thread, soonest due first."""
        ...

    async def queue_pending(self, channel_id: str, thread_ts: str) -> Run | None:
        """Queue the waiting thread's drafting run and forget the thread, atomically.

        The run coalesces with a queued one, as :meth:`enqueue_run` does.
        Returns None, queuing nothing, if the thread was not waiting, or if a
        person has since marked its first message as chatter: a later skip
        cancels the wait, and the thread is forgotten (#56).
        """
        ...

    async def drop_pending(self, channel_id: str, thread_ts: str) -> None:
        """Forget a waiting thread without queuing anything."""
        ...

    async def overdue_runs(self) -> list[Run]:
        """Running runs past their deadline, oldest deadline first."""
        ...

    async def recover_runs(self) -> Recovery:
        """At startup, in one transaction, settle every run a restart interrupted.

        Queued and running runs become interrupted. Each thread that had an
        interrupted drafting run gets one fresh drafting run. Every approved,
        executing, or reconciling proposal without a live execution run gets
        one, which also covers a crash between approving and queuing. An
        executing proposal first moves to reconciling, through the shared
        transition rules, because its write may have started.
        """
        ...

    # Write-tool ledger (#15): what each run's tool calls did, for idempotency and caps
    async def find_tool_call(self, run_id: str, tool: str, args_hash: str) -> str | None:
        """The result JSON of an identical earlier call by this run, if any."""
        ...

    async def record_tool_call(self, call: ToolCallRecord) -> int:
        """Record a call and return its sequence number within the run, from 1."""
        ...

    async def count_tool_calls(self, run_id: str, tools: Collection[str]) -> int:
        """How many of ``tools`` the run has called."""
        ...

    async def count_thread_calls(
        self, channel_id: str, thread_ts: str | None, tool: str, *, since: datetime
    ) -> int:
        """How many calls of ``tool`` any run made in this thread since ``since``."""
        ...

    # Packet membership (#14, #15): which message pointers a run's packet served
    async def record_packet(self, run_id: str, refs: Collection[MessageRef]) -> None:
        """Remember that ``run_id``'s packet served ``refs``."""
        ...

    async def packet_ref(self, run_id: str, channel_id: str, ts: str) -> MessageRef | None:
        """The pointer ``run_id``'s packet served at ``channel_id`` and ``ts``, or None."""
        ...
