"""The card's repo dropdown, its board menu (#309), and its Make assignee and Remove
buttons (#160).

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
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from jotflow.board_catalog import BoardCatalog, BoardEntry
from jotflow.models import IssueProposal, ProposalRecord, ProposedBoard, UpdateProposal
from jotflow.orchestrator.approval import Click, Refused
from jotflow.orchestrator.cards import (
    ACTION_ASSIGN,
    ACTION_BOARDS,
    ACTION_REPO,
    ACTION_UNASSIGN,
    BOARDS_BLOCK,
    MAX_CARD_BOARDS,
)
from jotflow.policy import NICKNAME_PATTERN, Policy, PolicyViolation, RepoPolicy
from jotflow.repo_catalog import RepoCatalog
from jotflow.store.base import (
    ADDED_ON_CARD,
    EDITABLE_STATES,
    EditField,
    Store,
    TransitionError,
)

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
NOT_A_CHANNEL_BOARD = "That board is not one this channel lists."
BOARDS_LOADING = (
    "JotFlow is still finding this channel's boards after a restart. Try again in a minute."
)
BOARD_OFF = "JotFlow's write token can't update that board, so it can't be chosen."
TOO_MANY_BOARDS = f"Choose at most {MAX_CARD_BOARDS} boards."
UPDATE_HAS_NO_BOARDS = "An update to an existing issue doesn't change its boards."

# The menu's block id, "jotflow_boards:<proposal id>", and a chosen option's value,
# a board nickname. Anything else is a malformed payload.
_BOARDS_BLOCK = re.compile(rf"{BOARDS_BLOCK}:(?P<id>[0-9a-f]{{16}})")


class Tell(Protocol):
    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None: ...


def assignees_of(draft: IssueProposal | UpdateProposal) -> list[str]:
    return list(
        draft.assignees if isinstance(draft, IssueProposal) else draft.changes.add_assignees
    )


def with_assignees(
    draft: IssueProposal | UpdateProposal, logins: list[str], added: str | None = None
) -> IssueProposal | UpdateProposal:
    """The draft with ``logins`` as its assignees. Each keeps its reason, a removed one's
    goes, and ``added``, a person's add from the card, says so (#305)."""
    old = (
        draft.assignee_reasons
        if isinstance(draft, IssueProposal)
        else draft.changes.assignee_reasons
    )
    wanted = {login.casefold() for login in logins}
    reasons = [r.model_dump() for r in old if r.login.casefold() in wanted]
    if added is not None and added.casefold() not in {r["login"].casefold() for r in reasons}:
        reasons.append({"login": added, "why": "added_on_card"})
    if isinstance(draft, IssueProposal):
        return IssueProposal.model_validate(
            {**draft.model_dump(), "assignees": logins, "assignee_reasons": reasons}
        )
    changes = draft.changes.model_dump() | {"add_assignees": logins, "assignee_reasons": reasons}
    return UpdateProposal.model_validate({**draft.model_dump(), "changes": changes})


def _boards_click(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """``body`` with its board menu action as the gate reads one: a value of
    "<proposal id>:<nicknames>". A malformed menu gets no value, so the gate
    ignores it."""
    actions = body.get("actions")
    action = actions[0] if isinstance(actions, list) and actions else None
    if not isinstance(action, Mapping):
        return body
    block = action.get("block_id")
    found = _BOARDS_BLOCK.fullmatch(block) if isinstance(block, str) else None
    options = action.get("selected_options")
    if found is None or not isinstance(options, list) or len(options) > MAX_CARD_BOARDS:
        return {**body, "actions": [{}]}
    nicknames = [o.get("value") if isinstance(o, Mapping) else None for o in options]
    if not all(isinstance(n, str) and NICKNAME_PATTERN.fullmatch(n) for n in nicknames):
        return {**body, "actions": [{}]}
    value = f"{found['id']}:{','.join(str(n) for n in nicknames)}"
    return {**body, "actions": [{"value": value}]}


class CardEdits:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        guarded: Guarded,
        tell: Tell,
        *,
        refresh: Refresh | None = None,
        repos: RepoCatalog | None = None,
        boards: BoardCatalog | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        # The channel's boards, listed and found, and which can be updated (#309).
        self._board_catalog = boards if boards is not None else BoardCatalog.static(policy)
        self._repos = repos if repos is not None else RepoCatalog.static(policy)
        self._guarded = guarded
        self._tell = tell
        self._refresh = refresh

    async def on_repo(self, body: Mapping[str, Any]) -> str:
        return await self._guarded(body, self._repo)

    async def on_assign(self, body: Mapping[str, Any]) -> str:
        return await self._guarded(body, self._assign)

    async def on_unassign(self, body: Mapping[str, Any]) -> str:
        return await self._guarded(body, self._unassign)

    async def on_boards(self, body: Mapping[str, Any]) -> str:
        """The board menu: its block names the proposal, and its options the boards.

        The gate reads one value, "<proposal id>:<payload>", so the menu's choice is
        put in that form: the nicknames, comma-separated, possibly none.
        """
        return await self._guarded(_boards_click(body), self._boards)

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
        logins = assignees_of(record.proposal)
        if link.github_login.casefold() in {login.casefold() for login in logins}:
            return await self._done(click, UNCHANGED, changed=False)
        edited = with_assignees(
            record.proposal, [*logins, link.github_login], added=link.github_login
        )
        await self._save(click, record, edited, "assign")
        return await self._done(click, f"Added {link.github_login} as an assignee.")

    async def _unassign(self, click: Click, record: ProposalRecord) -> str:
        login = self._check_may_edit(click, record)
        logins = assignees_of(record.proposal)
        kept = [existing for existing in logins if existing.casefold() != login.casefold()]
        if len(kept) == len(logins):
            return await self._done(click, UNCHANGED, changed=False)
        edited = with_assignees(record.proposal, kept)
        await self._save(click, record, edited, "unassign")
        return await self._done(click, "Removed that assignee.")

    async def _boards(self, click: Click, record: ProposalRecord) -> str:
        payload = self._check_may_edit(click, record)
        draft = record.proposal
        if not isinstance(draft, IssueProposal):
            raise Refused(UPDATE_HAS_NO_BOARDS)
        if self._board_catalog.awaiting(record.channel_id):
            # The menu couldn't offer found boards yet, so saving it could drop them.
            raise Refused(BOARDS_LOADING)
        wanted = [nickname for nickname in payload.split(",") if nickname]
        chosen: list[BoardEntry] = []
        for nickname in wanted:
            board = self._channel_board(record.channel_id, nickname)
            if board not in chosen:
                chosen.append(board)
        if len(chosen) > MAX_CARD_BOARDS:
            raise Refused(TOO_MANY_BOARDS)
        # A board the agent proposed keeps its reason; one a person added says so.
        reasons = {proposed.board: proposed.reason for proposed in draft.boards}
        boards = [
            ProposedBoard(board=b.board, reason=reasons.get(b.board, ADDED_ON_CARD)) for b in chosen
        ]
        # The order means nothing on the card or to the add, so only the set counts.
        if {b.board for b in boards} == {b.board for b in draft.boards}:
            return await self._done(click, UNCHANGED, changed=False)
        edited = draft.model_copy(update={"boards": boards})
        await self._save(click, record, edited, "boards")
        named = ", ".join(b.nickname for b in chosen)
        return await self._done(click, f"Boards: {named}." if named else "No boards.")

    # Helpers

    def _channel_board(self, channel_id: str, nickname: str) -> BoardEntry:
        """The menu sends a nickname. Only one of the channel's boards, listed or found
        in a fresh listing, by its nickname, is accepted, and only one the write
        token can update as far as JotFlow knows."""
        for board in self._board_catalog.boards(channel_id):
            if board.nickname == nickname:
                if not self._board_catalog.usable(board):
                    raise Refused(BOARD_OFF)
                return board
        raise Refused(NOT_A_CHANNEL_BOARD)

    def _channel_repo(self, channel_id: str, nickname: str) -> RepoPolicy:
        """The dropdown carries a nickname, unique in the channel. Nothing else is accepted:
        not an owner/name, and not a nickname the channel's repos and scopes don't give.
        A found repo is checked again, so an archived one is refused (#236)."""
        for entry in self._repos.repos(channel_id):
            if entry.nickname and entry.nickname == nickname:
                try:
                    return self._repos.repo(channel_id, entry.repo)
                except PolicyViolation as refused:
                    raise Refused(str(refused)) from None
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

    async def boards(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("boards", edits.on_boards(body))

    app.action(ACTION_REPO)(repo)
    app.action(ACTION_ASSIGN)(assign)
    app.action(ACTION_UNASSIGN)(unassign)
    app.action(ACTION_BOARDS)(boards)
