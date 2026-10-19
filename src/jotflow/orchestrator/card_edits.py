"""The card's repo dropdown and its Make assignee and Remove buttons (#160).

Who may click is Approve's rule (D7, #151), checked by the approval gate's
own ``guarded`` entry point: a member of this workspace, not a guest or an
external user, on the proposal's own card in its own channel, never in a
paused channel. With approvers on, only an approver may edit. Editing is not
approving, so a source author may.

An edit changes what would be approved, so it goes through the store's
``edit_proposal``. The content hash changes, an Approve from the old card is
refused as stale, and the card is redrawn. Only a pending or failed proposal
can be edited. Every reply is fixed text JotFlow builds, never the draft.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from jotflow.models import IssueProposal, ProposalRecord, UpdateProposal
from jotflow.orchestrator.approval import Click, Refused
from jotflow.orchestrator.cards import ACTION_ASSIGN, ACTION_REPO, ACTION_UNASSIGN
from jotflow.policy import Policy, RepoPolicy
from jotflow.store.base import EDITABLE_STATES, EditField, Store, TransitionError

log = logging.getLogger("jotflow.edits")

Action = Callable[[Click, ProposalRecord], Awaitable[str]]
Guarded = Callable[[Mapping[str, Any], Action], Awaitable[str]]
Refresh = Callable[[str], Awaitable[object]]

NOT_EDITABLE = "This proposal can no longer be changed here."
APPROVERS_ONLY = "Only the channel's approvers can change proposals here."
NOT_A_CHANNEL_REPO = "That repo is not one this channel feeds."
UPDATE_KEEPS_REPO = "An update to an existing issue stays in that issue's repo."
NOT_A_PARTICIPANT = "Only someone involved in this proposal can be made an assignee."
NOT_LINKED = (
    "That person hasn't linked their GitHub account, so they can't be made an assignee yet."
)
UNCHANGED = "Nothing changed."


class Tell(Protocol):
    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None: ...


def _assignees(draft: IssueProposal | UpdateProposal) -> list[str]:
    return list(
        draft.assignees if isinstance(draft, IssueProposal) else draft.changes.add_assignees
    )


def _with_assignees(
    draft: IssueProposal | UpdateProposal, logins: list[str]
) -> IssueProposal | UpdateProposal:
    if isinstance(draft, IssueProposal):
        return IssueProposal.model_validate({**draft.model_dump(), "assignees": logins})
    changes = draft.changes.model_dump() | {"add_assignees": logins}
    return UpdateProposal.model_validate({**draft.model_dump(), "changes": changes})


class CardEdits:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        guarded: Guarded,
        tell: Tell,
        *,
        refresh: Refresh | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._guarded = guarded
        self._tell = tell
        self._refresh = refresh

    async def on_repo(self, body: Mapping[str, Any]) -> str:
        return await self._guarded(body, self._repo)

    async def on_assign(self, body: Mapping[str, Any]) -> str:
        return await self._guarded(body, self._assign)

    async def on_unassign(self, body: Mapping[str, Any]) -> str:
        return await self._guarded(body, self._unassign)

    # The edits

    def _check_may_edit(self, click: Click, record: ProposalRecord) -> str:
        if record.state not in EDITABLE_STATES or record.draft_cleared_at is not None:
            raise Refused(NOT_EDITABLE)
        approval = self._policy.approval
        if approval.approvers_enabled and click.user_id not in approval.approvers:
            raise Refused(APPROVERS_ONLY)
        _, _, payload = click.value.partition(":")
        return payload

    async def _repo(self, click: Click, record: ProposalRecord) -> str:
        wanted = self._check_may_edit(click, record)
        entry = self._channel_repo(record.channel_id, wanted)
        draft = record.proposal
        if not isinstance(draft, IssueProposal):
            raise Refused(UPDATE_KEEPS_REPO)
        if draft.repo.casefold() == entry.repo.casefold():
            return await self._done(click, UNCHANGED, changed=False)
        labels = list(draft.labels)
        kept = self._allowed_labels(entry, labels)
        # The agent's visibility report was about the old repo (#122).
        edited = IssueProposal.model_validate(
            {**draft.model_dump(), "repo": entry.repo, "labels": kept, "repo_visibility": None}
        )
        await self._save(click, record, edited, "repo")
        note = f"Moved to {entry.nickname}."
        if len(kept) < len(labels):
            dropped = len(labels) - len(kept)
            note += f" {dropped} label{'s' if dropped != 1 else ''} not allowed there were dropped."
        return await self._done(click, note)

    async def _assign(self, click: Click, record: ProposalRecord) -> str:
        user = self._check_may_edit(click, record)
        if user not in record.participants:
            raise Refused(NOT_A_PARTICIPANT)
        link = await self._store.get_identity(user)
        if link is None:
            raise Refused(NOT_LINKED)
        logins = _assignees(record.proposal)
        if link.github_login.casefold() in {login.casefold() for login in logins}:
            return await self._done(click, UNCHANGED, changed=False)
        edited = _with_assignees(record.proposal, [*logins, link.github_login])
        await self._save(click, record, edited, "assign")
        return await self._done(click, f"Added {link.github_login} as an assignee.")

    async def _unassign(self, click: Click, record: ProposalRecord) -> str:
        login = self._check_may_edit(click, record)
        logins = _assignees(record.proposal)
        kept = [existing for existing in logins if existing.casefold() != login.casefold()]
        if len(kept) == len(logins):
            return await self._done(click, UNCHANGED, changed=False)
        edited = _with_assignees(record.proposal, kept)
        await self._save(click, record, edited, "unassign")
        return await self._done(click, "Removed that assignee.")

    # Helpers

    def _channel_repo(self, channel_id: str, nickname: str) -> RepoPolicy:
        """The dropdown carries a nickname, unique in the channel. Nothing else is accepted:
        not an owner/name, and not a nickname this channel's policy does not list."""
        for entry in self._policy.channel(channel_id).repos:
            if entry.nickname == nickname:
                return entry
        raise Refused(NOT_A_CHANNEL_REPO)

    @staticmethod
    def _allowed_labels(entry: RepoPolicy, labels: list[str]) -> list[str]:
        if not entry.restricts_labels:
            return labels
        allowed = {label.casefold(): label for label in entry.labels or ()}
        return [allowed[label.casefold()] for label in labels if label.casefold() in allowed]

    async def _save(
        self,
        click: Click,
        record: ProposalRecord,
        edited: IssueProposal | UpdateProposal,
        field: EditField,
    ) -> None:
        try:
            await self._store.edit_proposal(record.id, edited, by=click.user_id, field=field)
        except TransitionError:
            raise Refused(NOT_EDITABLE) from None
        log.info("proposal %s: %s edited by user %s", record.id, field, click.user_id)
        if self._refresh is not None:
            await self._refresh(record.id)

    async def _done(self, click: Click, text: str, *, changed: bool = True) -> str:
        if click.channel_id:
            await self._tell.ephemeral(click.channel_id, click.user_id, text)
        return "edited" if changed else "unchanged"


def register_edits(app: Any, edits: CardEdits) -> None:
    """Answer the card's edit controls. Each acks first, within Slack's three seconds."""

    async def run(name: str, work: Awaitable[object]) -> None:
        try:
            await work
        except Exception:
            log.exception("card edit %s failed", name)

    async def repo(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("repo", edits.on_repo(body))

    async def assign(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("assign", edits.on_assign(body))

    async def unassign(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("unassign", edits.on_unassign(body))

    app.action(ACTION_REPO)(repo)
    app.action(ACTION_ASSIGN)(assign)
    app.action(ACTION_UNASSIGN)(unassign)
