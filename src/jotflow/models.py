"""Shared data contracts.

These Pydantic models are the single source of truth for three things at
once: the MCP tool schemas the agent sees, the validation applied before
any GitHub write, and the shape rendered into Slack approval cards.

Only the orchestrator writes to GitHub. The agent emits proposals; the
orchestrator decides, based on the channel mode, whether a proposal is
applied immediately or held for human approval.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ChannelMode(StrEnum):
    """How a channel treats agent proposals. Applies to the full issue lifecycle."""

    PROPOSE = "propose"  # post a card and wait for approval
    AUTO = "auto"  # write immediately, then post a summary card
    PAUSED = "paused"  # read nothing, write nothing


class ProposalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class MessageRef(BaseModel):
    """A pointer to a Slack message. JotFlow stores these, never message bodies."""

    channel_id: str
    ts: str = Field(description="Slack message timestamp, unique within a channel")
    thread_ts: str | None = Field(default=None, description="Parent timestamp if in a thread")
    permalink: str | None = None


class IssueProposal(BaseModel):
    """A request to create a new GitHub issue."""

    kind: Literal["create_issue"] = "create_issue"
    topic_id: str
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$", description="owner/name")
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
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    issue_number: int = Field(gt=0)
    changes: IssueChanges
    sources: list[MessageRef] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


Proposal = IssueProposal | UpdateProposal


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
