"""The exact GitHub writes for an approved proposal (#31).

JotFlow never writes to GitHub (D5), but it decides exactly what the agent
writes: each GitHub MCP tool call with its arguments, the issue body with
JotFlow's footer and hidden marker, and the checks to make first. The agent
checks, then copies; tests compare what it wrote with these, exactly.

Everything here is pure. The footer's display names come from Slack, so they
are cleaned before they go into a GitHub body.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from jotflow.models import IssueProposal, ProposalRecord, UpdateProposal

MARKER_PREFIX = "jotflow:proposal:"
MAX_NAME = 40
MAX_NAMES = 10
# Anything that could mention, link, format, or break a line in GitHub markdown.
# ":" and "/" too, so a name cannot hold a URL GitHub would autolink.
_UNSAFE = re.compile(r"[@`\[\]<>#*_~|\\!():/\r\n\t]")
_SPACES = re.compile(r"\s+")
_WWW = re.compile(r"(?i)\bwww\.")


def marker(proposal_id: str) -> str:
    """The hidden HTML comment that marks a write as this proposal's."""
    return f"<!-- {MARKER_PREFIX}{proposal_id} -->"


def clean_name(name: str) -> str:
    """A Slack display name made safe as plain text in a GitHub body."""
    text = _SPACES.sub(" ", _WWW.sub("www ", _UNSAFE.sub(" ", name))).strip()
    return text[:MAX_NAME].rstrip()


def _joined(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def footer(
    proposal_id: str,
    *,
    poster: str | None,
    others: Sequence[str] = (),
    permalinks: Sequence[str] = (),
    issue_links: Literal["permalinks", "none"] = "permalinks",
) -> str:
    """JotFlow's attribution and marker, appended to every body and comment it plans.

    With ``issue_links = "none"`` it is the marker alone: no names, no links.
    """
    lines: list[str] = []
    if issue_links == "permalinks":
        who = clean_name(poster) if poster else ""
        looped = [n for n in (clean_name(o) for o in others) if n and n != who][:MAX_NAMES]
        if who and looped:
            lines.append(f"Raised in Slack by {who}, with {_joined(looped)} looped in.")
        elif who:
            lines.append(f"Raised in Slack by {who}.")
        elif looped:
            lines.append(f"Raised in Slack, with {_joined(looped)} looped in.")
        else:
            lines.append("Raised in Slack.")
        links = [link for link in permalinks if _is_permalink(link)]
        if links:
            lines.append("Slack: " + ", ".join(links))
    lines.append(marker(proposal_id))
    return "\n".join(lines)


_PERMALINK = re.compile(
    r"https://[a-z0-9-]+\.slack\.com/archives/[CG][A-Z0-9]+/p[0-9]{16}"
    r"(\?thread_ts=[0-9]{10}\.[0-9]{6}&cid=[CG][A-Z0-9]+)?"
)


def permalink(workspace: str, channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    """A Slack message link, built the way Slack builds them, from the workspace URL."""
    link = f"{workspace.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        link += f"?thread_ts={thread_ts}&cid={channel_id}"
    return link


def _is_permalink(link: str) -> bool:
    return bool(_PERMALINK.fullmatch(link))


def _with_footer(text: str, tail: str) -> str:
    return f"{text.rstrip()}\n\n---\n{tail}" if text.strip() else tail


class SetChange(BaseModel):
    """Labels or assignees to change on an existing issue, against its current set."""

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class PlannedWrite(BaseModel):
    """One GitHub MCP tool call to make exactly as given."""

    tool: Literal["issue_write", "add_issue_comment"]
    arguments: dict[str, Any] = Field(description="Pass these exactly; change nothing")
    labels_from_current: SetChange | None = Field(
        default=None,
        description=("Read the issue's current labels first; pass labels = current + add - remove"),
    )
    assignees_from_current: SetChange | None = Field(
        default=None,
        description=(
            "Read the issue's current assignees first; pass assignees = current + add - remove"
        ),
    )


class PlannedChecks(BaseModel):
    """What to read before writing."""

    marker: str = Field(description="Look for this text before writing anything")
    marker_in: Literal["recent_issues", "issue_comments", "issue_body", "nowhere"] = Field(
        description=(
            "Where an earlier attempt would have left it: the repo's issues (list_issues, "
            "no state filter, newest first), the issue's comments, or its body"
        )
    )
    issue_number: int | None = None
    labels: list[str] = Field(
        default_factory=list,
        description="Confirm each exists with get_label; if any is missing, write nothing",
    )


def _owner_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def plan(record: ProposalRecord, tail: str) -> tuple[PlannedChecks, list[PlannedWrite]]:
    """The checks and writes for ``record``, with ``tail`` from :func:`footer`."""
    draft = record.proposal
    owner, name = _owner_repo(draft.repo)
    mark = marker(record.id)
    if isinstance(draft, IssueProposal):
        arguments: dict[str, Any] = {
            "method": "create",
            "owner": owner,
            "repo": name,
            "title": draft.title,
            "body": _with_footer(draft.body, tail),
        }
        if draft.labels:
            arguments["labels"] = list(draft.labels)
        if draft.assignees:
            arguments["assignees"] = list(draft.assignees)
        checks = PlannedChecks(marker=mark, marker_in="recent_issues", labels=list(draft.labels))
        return checks, [PlannedWrite(tool="issue_write", arguments=arguments)]
    return _plan_update(record.id, draft, owner, name, tail)


def _plan_update(
    proposal_id: str, draft: UpdateProposal, owner: str, name: str, tail: str
) -> tuple[PlannedChecks, list[PlannedWrite]]:
    changes = draft.changes
    mark = marker(proposal_id)
    writes: list[PlannedWrite] = []
    edit: dict[str, Any] = {
        "method": "update",
        "owner": owner,
        "repo": name,
        "issue_number": draft.issue_number,
    }
    labels = (
        SetChange(add=list(changes.add_labels), remove=list(changes.remove_labels))
        if changes.add_labels or changes.remove_labels
        else None
    )
    assignees = (
        SetChange(add=list(changes.add_assignees), remove=list(changes.remove_assignees))
        if changes.add_assignees or changes.remove_assignees
        else None
    )
    if changes.body is not None:
        # The marker goes in the body only when there is no comment to carry it.
        edit["body"] = (
            changes.body if changes.comment is not None else _with_footer(changes.body, mark)
        )
    if changes.body is not None or labels or assignees:
        writes.append(
            PlannedWrite(
                tool="issue_write",
                arguments=edit,
                labels_from_current=labels,
                assignees_from_current=assignees,
            )
        )
    if changes.comment is not None:
        writes.append(
            PlannedWrite(
                tool="add_issue_comment",
                arguments={
                    "owner": owner,
                    "repo": name,
                    "issue_number": draft.issue_number,
                    "body": _with_footer(changes.comment, tail),
                },
            )
        )
    where: Literal["issue_comments", "issue_body", "nowhere"] = (
        "issue_comments"
        if changes.comment is not None
        else "issue_body"
        if changes.body is not None
        else "nowhere"
    )
    checks = PlannedChecks(
        marker=mark,
        marker_in=where,
        issue_number=draft.issue_number,
        labels=list(changes.add_labels),
    )
    return checks, writes
