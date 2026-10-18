"""The Block Kit card for a proposal (#26).

A pending proposal appears in its thread as a card that shows everything a
person approves (D7): the full issue body, the repo, labels, assignees, size,
and confidence. The same card is the summary after an auto-mode write, and it
is edited in place as the proposal changes state.

Everything the agent wrote, and everything that came from Slack, goes into
``plain_text`` objects, which Slack never parses for mentions, links, or
formatting. ``mrkdwn`` is used only for text this module builds itself from
validated ids and links. The Approve button carries the content hash of the
draft it shows, so approving a stale card fails (D7).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Literal, Protocol

from jotflow.models import (
    IssueProposal,
    ProposalRecord,
    ProposalState,
    UpdateProposal,
)
from jotflow.policy import ChannelPolicy, RepoPolicy
from jotflow.store.base import AUTO_APPROVER, IdentityLink, Store
from jotflow.store.hashing import content_hash

Block = dict[str, Any]
Visibility = Literal["public", "private", "unknown"]

# Slack's limits.
MAX_BLOCKS = 50
MAX_SECTION_TEXT = 3000
MAX_HEADER_TEXT = 150
MAX_CONTEXT_TEXT = 2000
MAX_BUTTON_TEXT = 75
MAX_OPTION_TEXT = 75

ACTION_APPROVE = "jotflow_approve"
ACTION_REQUEST_CHANGES = "jotflow_request_changes"
ACTION_REPO = "jotflow_repo"
ACTION_ASSIGN = "jotflow_assign"
ACTION_UNASSIGN = "jotflow_unassign"

_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")
_PERMALINK = re.compile(
    r"https://[a-z0-9-]+\.slack\.com/archives/[A-Za-z0-9]+/p[0-9]+(\?[A-Za-z0-9=&._-]*)?"
)

_STATE_LINES = {
    ProposalState.EXECUTING: "Creating it on GitHub now.",
    ProposalState.RECONCILING: "The write was interrupted; checking whether it already happened.",
    ProposalState.REJECTED: "Rejected. Nothing was written.",
    ProposalState.SUPERSEDED: "Replaced by a newer proposal in this thread.",
}


@dataclass(frozen=True)
class CardContext:
    """What the card needs besides the proposal itself."""

    channel: ChannelPolicy
    names: Mapping[str, str] = field(default_factory=dict)  # Slack user id -> display name
    identities: Mapping[str, IdentityLink] = field(default_factory=dict)  # by Slack user id
    visibility: Mapping[str, Visibility] = field(default_factory=dict)  # by owner/name, any case

    def repo_visibility(self, repo: str) -> Visibility:
        for name, value in self.visibility.items():
            if name.casefold() == repo.casefold():
                return value
        return "unknown"

    def login_link(self, login: str) -> IdentityLink | None:
        for link in self.identities.values():
            if link.github_login.casefold() == login.casefold():
                return link
        return None


@dataclass(frozen=True)
class Card:
    blocks: list[Block]
    text: str  # notification fallback; holds no agent-written text


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plain(text: str, limit: int = MAX_SECTION_TEXT) -> dict[str, Any]:
    """A plain_text object: Slack shows it literally, so nothing in it can ping or link."""
    return {"type": "plain_text", "text": _cut(text, limit) or " ", "emoji": False}


def _section(text: str) -> Block:
    return {"type": "section", "text": plain(text)}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [plain(text, MAX_CONTEXT_TEXT)]}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "text": plain(text, MAX_BUTTON_TEXT),
        "action_id": action_id,
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def _chunks(text: str, size: int = MAX_SECTION_TEXT) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _nickname(channel: ChannelPolicy, repo: str) -> str:
    for entry in channel.repos:
        if entry.repo.casefold() == repo.casefold():
            return entry.nickname
    return repo


def _repo_entry(channel: ChannelPolicy, repo: str) -> RepoPolicy | None:
    for entry in channel.repos:
        if entry.repo.casefold() == repo.casefold():
            return entry
    return None


def _repo_option(entry: RepoPolicy) -> dict[str, Any]:
    return {"text": plain(f"{entry.nickname}: {entry.repo}", MAX_OPTION_TEXT), "value": entry.repo}


def _repo_blocks(record: ProposalRecord, context: CardContext, pending: bool) -> list[Block]:
    repo = record.proposal.repo
    entry = _repo_entry(context.channel, repo)
    blocks: list[Block] = []
    if pending and entry is not None and len(context.channel.repos) > 1:
        blocks.append(
            {
                "type": "section",
                "text": plain("Repo"),
                "accessory": {
                    "type": "static_select",
                    "action_id": ACTION_REPO,
                    "initial_option": _repo_option(entry),
                    "options": [_repo_option(r) for r in context.channel.repos],
                },
            }
        )
    else:
        blocks.append(_section(f"Repo: {repo}"))
    visibility = context.repo_visibility(repo)
    if visibility == "public":
        blocks.append(
            _context(f"Warning: {repo} is public, so this issue will be visible to anyone.")
        )
    elif visibility == "unknown":
        blocks.append(_context("Repo visibility was not checked."))
    return blocks


def _labels_line(labels: Sequence[str], entry: RepoPolicy | None, prefix: str = "Labels") -> str:
    shown = ", ".join(labels) if labels else "none"
    checked = entry is not None and entry.restricts_labels
    return f"{prefix}: {shown}" + ("" if checked or not labels else " (not checked against a list)")


def _people_blocks(
    record: ProposalRecord, context: CardContext, assignees: Sequence[str], pending: bool
) -> list[Block]:
    blocks: list[Block] = []
    for login in assignees:
        link = context.login_link(login)
        note = (
            ""
            if link is not None and link.verified
            else " (unverified)"
            if link is not None
            else " (not linked to anyone in Slack)"
        )
        block = _section(f"Assignee: {login}{note}")
        if pending:
            block["accessory"] = _button("Remove", ACTION_UNASSIGN, f"{record.id}:{login}")
        blocks.append(block)
    taken = {login.casefold() for login in assignees}
    for user in record.participants:
        name = context.names.get(user, user)
        link = context.identities.get(user)
        block = _section(f"Involved: {name}")
        if pending and link is not None and link.github_login.casefold() not in taken:
            block["accessory"] = _button("Make assignee", ACTION_ASSIGN, f"{record.id}:{user}")
        blocks.append(block)
    return blocks


TOO_LONG = (
    "This draft is too long to show in full on one card, so it cannot be approved here. "
    "Use Request Changes to ask for a shorter one."
)


def _body(text: str, show: bool) -> list[Block]:
    return [_section(chunk) for chunk in _chunks(text)] if show else [_context(TOO_LONG)]


def _draft_blocks(
    record: ProposalRecord, context: CardContext, pending: bool, show_body: bool = True
) -> tuple[list[Block], str]:
    """The blocks for the draft's content, and the header text."""
    draft = record.proposal
    entry = _repo_entry(context.channel, draft.repo)
    if isinstance(draft, IssueProposal):
        header = "Proposed issue"
        blocks: list[Block] = []
        if record.draft_cleared_at is None:
            blocks.append(_section(draft.title))
            blocks += _body(draft.body, show_body)
        blocks.append(_context(_labels_line(draft.labels, entry)))
        details = [f"Confidence: {round(draft.confidence * 100)}%"]
        if draft.size:
            details.insert(0, f"Size: {draft.size}")
        blocks.append(_context("   ".join(details)))
        blocks += _people_blocks(record, context, draft.assignees, pending)
        return blocks, header
    header = f"Proposed update to {_nickname(context.channel, draft.repo)}#{draft.issue_number}"
    return _update_blocks(record, draft, context, entry, pending, show_body), header


def _update_blocks(
    record: ProposalRecord,
    draft: UpdateProposal,
    context: CardContext,
    entry: RepoPolicy | None,
    pending: bool,
    show_body: bool,
) -> list[Block]:
    changes = draft.changes
    blocks: list[Block] = []
    if record.draft_cleared_at is None:
        if changes.body is not None:
            blocks.append(_context("New description:"))
            blocks += _body(changes.body, show_body)
        if changes.comment is not None:
            blocks.append(_context("Comment:"))
            blocks += _body(changes.comment, show_body)
    if changes.add_labels:
        blocks.append(_context(_labels_line(changes.add_labels, entry, "Add labels")))
    if changes.remove_labels:
        blocks.append(_context("Remove labels: " + ", ".join(changes.remove_labels)))
    if changes.remove_assignees:
        blocks.append(_context("Unassign: " + ", ".join(changes.remove_assignees)))
    blocks.append(_context(f"Confidence: {round(draft.confidence * 100)}%"))
    blocks += _people_blocks(record, context, changes.add_assignees, pending)
    return blocks


def _sources_block(record: ProposalRecord) -> Block | None:
    links = [
        ref.permalink
        for ref in record.proposal.sources
        if ref.permalink and _PERMALINK.fullmatch(ref.permalink)
    ]
    if not links:
        return None
    text = "Drawn from " + ", ".join(f"<{url}|message {i}>" for i, url in enumerate(links, 1))
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": _cut(text, MAX_CONTEXT_TEXT)}],
    }


def _state_block(record: ProposalRecord) -> Block | None:
    state = record.state
    if state is ProposalState.PENDING:
        return None
    if record.draft_cleared_at is not None and state is not ProposalState.APPLIED:
        suffix = " The drafted text has been cleared."
    else:
        suffix = ""
    if state is ProposalState.APPROVED:
        return _approved_block(record)
    if state is ProposalState.APPLIED and record.result and record.result.issue_url:
        url = record.result.issue_url
        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Done: <{url}|{_issue_label(url)}>"}],
        }
    if state is ProposalState.FAILED:
        reason = record.result.error if record.result and record.result.error else None
        line = "The write failed." + (f" The agent said: {reason}" if reason else "")
        return _context(line + suffix)
    return _context(_STATE_LINES.get(state, state.value) + suffix)


def _issue_label(url: str) -> str:
    owner_repo_number = url.removeprefix("https://github.com/")
    owner_repo, _, number = owner_repo_number.partition("/issues/")
    return f"{owner_repo}#{number.split('#', 1)[0]}"


def _approved_block(record: ProposalRecord) -> Block:
    when = (
        record.approved_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if record.approved_at
        else ""
    )
    who = record.approved_by or ""
    if who == AUTO_APPROVER:
        return _context(f"Approved by auto mode {when}".strip())
    if _SLACK_USER.fullmatch(who):
        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Approved by <@{who}> {when}".strip()}],
        }
    return _context(f"Approved {when}".strip())


def render_card(record: ProposalRecord, context: CardContext) -> Card:
    """The card for ``record`` in its current state."""
    blocks, header, fits = _assemble(record, context, show_body=True)
    if not fits:
        # A draft too long to show in full cannot be approved from the card (D7).
        blocks, header, _ = _assemble(record, context, show_body=False)
    if record.state is ProposalState.PENDING:
        actions = [_button("Request Changes", ACTION_REQUEST_CHANGES, record.id)]
        if fits:
            approve = _button(
                "Approve",
                ACTION_APPROVE,
                f"{record.id}:{content_hash(record.proposal)}",
                style="primary",
            )
            actions.insert(0, approve)
        blocks.append({"type": "actions", "elements": actions})
    return Card(blocks=blocks, text=f"{header} for {record.proposal.repo}")


def _assemble(
    record: ProposalRecord, context: CardContext, *, show_body: bool
) -> tuple[list[Block], str, bool]:
    pending = record.state is ProposalState.PENDING
    content, header = _draft_blocks(record, context, pending, show_body)
    blocks: list[Block] = [{"type": "header", "text": plain(header, MAX_HEADER_TEXT)}]
    blocks += _repo_blocks(record, context, pending)
    blocks += content
    if (sources := _sources_block(record)) is not None:
        blocks.append(sources)
    if (state := _state_block(record)) is not None:
        blocks.append(state)
    reserved = 1 if pending else 0  # the actions block
    return blocks, header, len(blocks) + reserved <= MAX_BLOCKS


class CardPoster(Protocol):
    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        """Post the card into the thread; return the new message's ts."""
        ...

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        """Replace the card message at ``ts``."""
        ...


class SlackCardPoster:
    """Posts and edits cards with the Slack Web API."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        response = await self._client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, blocks=card.blocks, text=card.text
        )
        return str(response["ts"])

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        await self._client.chat_update(
            channel=channel_id, ts=ts, blocks=card.blocks, text=card.text
        )


class CardPublisher:
    """Shows a proposal's card: posts it the first time, then edits it in place."""

    def __init__(self, store: Store, poster: CardPoster) -> None:
        self._store = store
        self._poster = poster

    async def show(self, record: ProposalRecord, context: CardContext) -> str:
        """Post or update the card; return its message ts."""
        card = render_card(record, context)
        if record.card_ts:
            await self._poster.update(record.channel_id, record.card_ts, card)
            return record.card_ts
        ts = await self._poster.post(record.channel_id, record.thread_ts, card)
        await self._store.set_card_ts(record.id, ts)
        return ts
