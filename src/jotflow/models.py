"""Shared data contracts.

These Pydantic models are the single source of truth for three things at
once: the MCP tool schemas the agent sees, the validation applied before a
proposal card is posted, and the shape rendered into Slack approval cards.

JotFlow never writes to GitHub. The agent drafts proposals through JotFlow's
tools; JotFlow decides, based on the channel mode, whether a proposal waits
for a person or is approved automatically. Once approved, JotFlow launches an
execution run in which the agent performs the write with its own GitHub
access and reports back with :class:`ExecutionResult` (D5, D8).

Models the agent fills in (:class:`IssueProposal`, :class:`UpdateProposal`,
:class:`Classification`, :class:`ExecutionResult`) carry only what the agent
may decide. Ids, channel, run, and state are assigned by JotFlow and live on
:class:`ProposalRecord` and :class:`Run`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"

_ISSUE_URL = re.compile(
    r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<number>[1-9]\d*)"
    r"(?:#issuecomment-\d+)?",
    re.ASCII,
)


class ChannelMode(StrEnum):
    """How a channel treats agent proposals. Applies to the full issue lifecycle."""

    PROPOSE = "propose"  # post a card and wait for approval
    AUTO = "auto"  # approve immediately, write, then post the card
    PAUSED = "paused"  # read nothing, write nothing


class ProposalState(StrEnum):
    """Proposal lifecycle; see the state diagram in docs/architecture.md."""

    PENDING = "pending"  # card posted, waiting for a person
    APPROVED = "approved"  # content locked, waiting for an execution run
    EXECUTING = "executing"  # an execution run is performing the write
    RECONCILING = "reconciling"  # a run was interrupted; checking for the marker
    APPLIED = "applied"  # the agent reported a matching issue URL
    REJECTED = "rejected"  # a person rejected or skipped it
    SUPERSEDED = "superseded"  # a newer proposal on the same thread replaced it
    FAILED = "failed"  # the write failed or the retry limit was reached


class RunKind(StrEnum):
    DRAFTING = "drafting"  # reads a thread, proposes; read-only GitHub access
    EXECUTION = "execution"  # performs one approved write
    QUESTION = "question"  # answers a read-only question in the thread (M5, D12)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"  # JotFlow restarted while the run was in flight


class MessageRef(BaseModel):
    """A pointer to a Slack message. JotFlow stores these, never message bodies."""

    channel_id: str
    ts: str = Field(description="Slack message timestamp, unique within a channel")
    thread_ts: str | None = Field(default=None, description="Parent timestamp if in a thread")
    permalink: str | None = None


class IssueProposal(BaseModel):
    """A request to create a new GitHub issue."""

    kind: Literal["create_issue"] = "create_issue"
    topic_id: str | None = Field(
        default=None,
        description="Existing topic this issue is for; omit to start a new topic",
    )
    repo: str = Field(pattern=REPO_PATTERN, description="owner/name")
    title: str = Field(min_length=1, max_length=256)
    body: str
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list, description="GitHub logins")
    project: str | None = None
    size: str | None = None
    sources: list[MessageRef] = Field(
        default_factory=list, description="Slack messages this proposal was drawn from"
    )
    confidence: float = Field(ge=0.0, le=1.0)


class IssueChanges(BaseModel):
    """The subset of an issue an update may touch. All fields optional."""

    body: str | None = None
    comment: str | None = None
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    add_assignees: list[str] = Field(default_factory=list)
    remove_assignees: list[str] = Field(default_factory=list)


class UpdateProposal(BaseModel):
    """A request to change an existing GitHub issue."""

    kind: Literal["update_issue"] = "update_issue"
    topic_id: str
    repo: str = Field(pattern=REPO_PATTERN)
    issue_number: int = Field(gt=0)
    changes: IssueChanges
    sources: list[MessageRef] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


Proposal = Annotated[IssueProposal | UpdateProposal, Field(discriminator="kind")]


class ExecutionResult(BaseModel):
    """What the agent reports through ``record_result`` after an execution run.

    Exactly one of ``issue_url`` and ``error`` is set.
    """

    issue_url: str | None = Field(
        default=None, description="https://github.com/owner/name/issues/N of the issue written"
    )
    error: str | None = Field(
        default=None, max_length=2000, description="Why the write failed, if it did"
    )

    @model_validator(mode="after")
    def _one_outcome(self) -> ExecutionResult:
        if (self.issue_url is None) == (self.error is None):
            raise ValueError("set exactly one of issue_url and error")
        if self.issue_url is not None and not _ISSUE_URL.fullmatch(self.issue_url):
            raise ValueError("issue_url must look like https://github.com/owner/name/issues/N")
        return self

    def issue_ref(self) -> tuple[str, int] | None:
        """The ``(owner/name, number)`` the URL points at, or None for an error result."""
        if self.issue_url is None:
            return None
        match = _ISSUE_URL.fullmatch(self.issue_url)
        if match is None:  # pragma: no cover - rejected by the validator
            return None
        return match["repo"], int(match["number"])

    def points_at(self, repo: str, issue_number: int | None = None) -> bool:
        """Whether the URL is in ``repo`` and, if given, is issue ``issue_number``.

        Repo names compare without case, as GitHub does.
        """
        ref = self.issue_ref()
        if ref is None:
            return False
        url_repo, url_number = ref
        if url_repo.casefold() != repo.casefold():
            return False
        return issue_number is None or url_number == issue_number


class ProposalRecord(BaseModel):
    """A proposal as JotFlow stores and tracks it.

    The agent supplies only :attr:`proposal`; everything else is JotFlow's.
    """

    id: str = Field(
        description="Random id assigned by JotFlow; doubles as the hidden marker in the issue body"
    )
    channel_id: str
    thread_ts: str | None = Field(default=None, description="Thread the proposal came from")
    run_id: str = Field(description="The drafting run that produced it")
    state: ProposalState = ProposalState.PENDING
    proposal: Proposal
    content_hash: str | None = Field(
        default=None, description="Hash of the approved content; set when approved"
    )
    approved_by: str | None = Field(
        default=None, description="Slack user id of the approver, or 'auto' in auto mode"
    )
    approved_at: AwareDatetime | None = None
    result: ExecutionResult | None = None
    created_at: AwareDatetime


class Run(BaseModel):
    """One launch of the team's agent (D8)."""

    id: str
    kind: RunKind
    status: RunStatus = RunStatus.QUEUED
    channel_id: str
    thread_ts: str | None = None
    proposal_id: str | None = Field(
        default=None, description="Required for execution runs: the approved proposal"
    )
    created_at: AwareDatetime
    deadline: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _execution_has_proposal(self) -> Run:
        if self.kind is RunKind.EXECUTION and self.proposal_id is None:
            raise ValueError("an execution run needs proposal_id")
        return self


class Classification(BaseModel):
    """The agent's decision about where a Slack message belongs."""

    message: MessageRef
    decision: Literal["new_topic", "existing_topic", "chatter"]
    topic_id: str | None = Field(
        default=None, description="Required when decision is existing_topic"
    )
    confidence: float = Field(ge=0.0, le=1.0)


class Topic(BaseModel):
    """An active line of discussion in a channel and the issue it maps to, if any."""

    id: str
    channel_id: str
    title: str = Field(description="Short generated title; the only derived text JotFlow stores")
    repo: str | None = None
    issue_number: int | None = None
    messages: list[MessageRef] = Field(default_factory=list)
