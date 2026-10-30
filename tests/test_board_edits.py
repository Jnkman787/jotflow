"""The card's board menu (#309): a person changes the Projects boards from the card."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jotflow.board_catalog import BoardCatalog
from jotflow.models import IssueProposal, ProposalState, ProposedBoard
from jotflow.orchestrator.approval import STALE, ApprovalHandler
from jotflow.orchestrator.card_edits import (
    BOARD_OFF,
    NOT_A_CHANNEL_BOARD,
    TOO_MANY_BOARDS,
    UNCHANGED,
    UPDATE_HAS_NO_BOARDS,
    CardEdits,
)
from jotflow.orchestrator.cards import (
    ACTION_APPROVE,
    ACTION_BOARDS,
    BOARDS_BLOCK,
    CardContext,
    CardPublisher,
    render_card,
)
from jotflow.policy import Policy
from jotflow.store import MemoryStore, content_hash
from jotflow.store.base import ADDED_ON_CARD
from tests.test_approval import ALICE, BOB, CARD_TS, Cards, Slack
from tests.test_card_edits import PID, WEB, body, click
from tests.test_runs import CH, _proposal_record

BOARDS = [
    {"board": "acme-co/3", "nickname": "roadmap", "description": "Planned work"},
    {"board": "acme-co/7", "nickname": "support", "description": "Customer problems"},
    {"board": "acme-co/9", "nickname": "design", "description": "Design work"},
    {"board": "acme-co/11", "nickname": "infra", "description": "Infrastructure"},
]
POLICY = Policy.model_validate({"channels": {CH: {"repos": [WEB], "boards": BOARDS}}})

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


class Rig:
    def __init__(self, boards_off: frozenset[str] = frozenset()) -> None:
        self.store = MemoryStore()
        self.slack = Slack()
        self.cards = Cards()
        self.boards_off = boards_off
        self._publisher = CardPublisher(self.store, self.cards)
        self.handler = ApprovalHandler(self.store, POLICY, self.slack, refresh=self._refresh)
        self.edits = CardEdits(
            self.store,
            POLICY,
            self.handler.guarded,
            self.slack,
            refresh=self._refresh,
            boards=BoardCatalog.static(POLICY, off=boards_off),
        )

    async def _refresh(self, proposal_id: str) -> None:
        record = await self.store.get_proposal(proposal_id)
        assert record is not None
        await self._publisher.show(record, CardContext(channel=POLICY.channel(CH)))

    async def proposal(self, *boards: ProposedBoard) -> str:
        record = _proposal_record(PID)
        proposal = IssueProposal.model_validate(
            {**record.proposal.model_dump(), "repo": "acme-co/web", "boards": list(boards)}
        )
        record = record.model_copy(update={"participants": [ALICE, BOB], "proposal": proposal})
        await self.store.add_proposal(record)
        await self.store.set_card_ts(PID, CARD_TS)
        return PID

    async def boards(self) -> list[tuple[str, str]]:
        record = await self.store.get_proposal(PID)
        assert record is not None and isinstance(record.proposal, IssueProposal)
        return [(b.board, b.reason) for b in record.proposal.boards]

    def app(self) -> Any:
        from tests.test_card_edits import Rig as RepoRig

        app = RepoRig.app(self)  # type: ignore[arg-type]
        return app


def menu(*nicknames: str, block: str | None = None, **kw: Any) -> dict[str, Any]:
    """A board menu click, as Slack sends one: the chosen options, possibly none."""
    payload = body(ACTION_BOARDS, "", **kw)
    payload["actions"] = [
        {
            "action_id": ACTION_BOARDS,
            "block_id": block if block is not None else f"{BOARDS_BLOCK}:{PID}",
            "type": "multi_static_select",
            "selected_options": [
                {"value": n, "text": {"type": "plain_text", "text": n}} for n in nicknames
            ],
        }
    ]
    return payload


ROADMAP = ProposedBoard(board="acme-co/3", reason="Planned for next quarter")


# The card


def test_a_pending_card_offers_the_channels_boards_with_the_drafts_selected() -> None:
    record = _proposal_record(PID)
    draft = IssueProposal.model_validate({**record.proposal.model_dump(), "boards": [ROADMAP]})
    record = record.model_copy(update={"proposal": draft})
    context = CardContext(
        channel=POLICY.channel(CH),
        offered_boards=tuple(BoardCatalog.static(POLICY, {"acme-co/9"}).offered(CH)),
    )
    blocks = render_card(record, context).blocks
    [section] = [b for b in blocks if b.get("block_id") == f"{BOARDS_BLOCK}:{PID}"]
    select = section["accessory"]
    assert select["type"] == "multi_static_select" and select["action_id"] == ACTION_BOARDS
    assert select["max_selected_items"] == 3
    # A board the write token can't update is never offered.
    assert [o["value"] for o in select["options"]] == ["roadmap", "support", "infra"]
    assert [o["value"] for o in select["initial_options"]] == ["roadmap"]
    assert select["options"][0]["text"]["text"] == "roadmap: acme-co/3"
    text = json.dumps(blocks)
    assert "The agent's reason for roadmap (acme-co/3): Planned for next quarter" in text


def test_a_card_that_isnt_pending_has_no_board_menu_and_says_who_added_a_board() -> None:
    record = _proposal_record(PID)
    added = ProposedBoard(board="acme-co/7", reason=ADDED_ON_CARD)
    draft = IssueProposal.model_validate(
        {**record.proposal.model_dump(), "boards": [ROADMAP, added]}
    )
    record = record.model_copy(update={"proposal": draft, "state": ProposalState.APPROVED})
    blocks = render_card(record, CardContext(channel=POLICY.channel(CH))).blocks
    assert not [b for b in blocks if str(b.get("block_id", "")).startswith(BOARDS_BLOCK)]
    text = json.dumps(blocks)
    assert "Add to boards: roadmap (acme-co/3), support (acme-co/7)" in text
    assert "support (acme-co/7) was added on the card." in text
    assert "The agent's reason for support" not in text


def test_a_channel_without_boards_gets_no_menu() -> None:
    plain = Policy.model_validate({"channels": {CH: {"repos": [WEB]}}})
    blocks = render_card(_proposal_record(PID), CardContext(channel=plain.channel(CH))).blocks
    assert not [b for b in blocks if str(b.get("block_id", "")).startswith(BOARDS_BLOCK)]
    every_off = frozenset(b["board"] for b in BOARDS)
    context = CardContext(
        channel=POLICY.channel(CH),
        offered_boards=tuple(BoardCatalog.static(POLICY, every_off).offered(CH)),
    )
    blocks = render_card(_proposal_record(PID), context).blocks
    assert not [b for b in blocks if str(b.get("block_id", "")).startswith(BOARDS_BLOCK)]


# The edit


async def test_choosing_boards_keeps_the_agents_reasons_and_marks_a_persons() -> None:
    rig = Rig()
    await rig.proposal(ROADMAP)
    await click(rig.app(), menu("support", "roadmap"))
    assert await rig.boards() == [
        ("acme-co/7", ADDED_ON_CARD),
        ("acme-co/3", "Planned for next quarter"),
    ]
    assert rig.slack.ephemerals == [(CH, BOB, "Boards: support, roadmap.")]
    (audit,) = await rig.store.proposal_edits(PID)
    assert (audit.edited_by, audit.field) == (BOB, "boards")
    assert len(rig.cards.updates) == 1


async def test_emptying_the_menu_takes_every_board_off() -> None:
    rig = Rig()
    await rig.proposal(ROADMAP)
    await click(rig.app(), menu())
    assert await rig.boards() == []
    assert rig.slack.ephemerals == [(CH, BOB, "No boards.")]


async def test_the_same_boards_change_nothing() -> None:
    rig = Rig()
    await rig.proposal(ROADMAP)
    await click(rig.app(), menu("roadmap"))
    assert rig.slack.ephemerals == [(CH, BOB, UNCHANGED)]
    assert await rig.store.proposal_edits(PID) == []


async def test_reordering_the_same_boards_changes_nothing() -> None:
    # The order means nothing, so it adds no edit and leaves reactions on.
    rig = Rig()
    await rig.proposal(ROADMAP, ProposedBoard(board="acme-co/7", reason="Reported"))
    await click(rig.app(), menu("support", "roadmap"))
    assert rig.slack.ephemerals == [(CH, BOB, UNCHANGED)]
    assert await rig.store.proposal_edits(PID) == []
    assert [b for b, _ in await rig.boards()] == ["acme-co/3", "acme-co/7"]


@pytest.mark.parametrize(
    ("chosen", "refusal"),
    [
        (("roadmap", "elsewhere"), NOT_A_CHANNEL_BOARD),
        (("acme-co/3",), None),  # an owner/number isn't a nickname: malformed, ignored
        (("design",), BOARD_OFF),
    ],
)
async def test_only_the_channels_boards_the_token_can_update_are_accepted(
    chosen: tuple[str, ...], refusal: str | None
) -> None:
    rig = Rig(boards_off=frozenset({"acme-co/9"}))
    await rig.proposal(ROADMAP)
    await click(rig.app(), menu(*chosen))
    assert await rig.boards() == [("acme-co/3", "Planned for next quarter")]
    assert rig.slack.ephemerals == ([(CH, BOB, refusal)] if refusal else [])
    assert await rig.store.proposal_edits(PID) == []


async def test_more_than_three_boards_or_a_bad_block_is_never_saved() -> None:
    rig = Rig()
    await rig.proposal(ROADMAP)
    # Slack caps the menu at three; a payload with four is malformed.
    await click(rig.app(), menu("roadmap", "support", "design", "infra"))
    await click(rig.app(), menu("support", block="jotflow_boards:not-a-proposal"))
    await click(rig.app(), menu("support", block="b"))
    assert await rig.boards() == [("acme-co/3", "Planned for next quarter")]
    assert rig.slack.ephemerals == []
    assert TOO_MANY_BOARDS == "Choose at most 3 boards."


async def test_an_update_has_no_boards_to_change() -> None:
    from jotflow.models import IssueChanges, UpdateProposal

    rig = Rig()
    topic = await rig.store.create_topic(CH, "Export")
    update = UpdateProposal(
        topic_id=topic.topic.id,
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(comment="Keep the order."),
        confidence=0.9,
    )
    record = _proposal_record(PID).model_copy(update={"proposal": update})
    await rig.store.add_proposal(record)
    await rig.store.set_card_ts(PID, CARD_TS)
    await click(rig.app(), menu("roadmap"))
    assert rig.slack.ephemerals == [(CH, BOB, UPDATE_HAS_NO_BOARDS)]


async def test_an_approve_from_before_the_board_edit_is_stale() -> None:
    rig = Rig()
    await rig.proposal(ROADMAP)
    record = await rig.store.get_proposal(PID)
    assert record is not None
    seen = content_hash(record.proposal)
    await click(rig.app(), menu("roadmap", "support"))
    await click(rig.app(), body(ACTION_APPROVE, f"{PID}:{seen}", user=ALICE))
    after = await rig.store.get_proposal(PID)
    assert after is not None and after.state is ProposalState.PENDING
    assert (CH, ALICE, STALE) in rig.slack.ephemerals


# The store


async def test_the_migration_keeps_every_edit_and_allows_a_board_edit(tmp_path: Any) -> None:
    from jotflow.store import SqliteStore
    from jotflow.store.hashing import canonical_json
    from jotflow.store.migrations import MIGRATIONS
    from tests.sqlite_db import opened
    from tests.test_runs import START, THREAD

    path = tmp_path / "jotflow.db"
    # The store as it was before migration 19 rebuilt the edit table.
    old = await SqliteStore.open(path, migrations=[m for m in MIGRATIONS if m.version < 19])
    await old.close()
    draft = IssueProposal(repo="acme-co/web", title="T", body="B", confidence=0.5)
    with opened(path) as db:
        db.execute(
            "INSERT INTO proposals (id, channel_id, thread_ts, run_id, state, kind, repo,"
            " draft_json, created_at, updated_at)"
            " VALUES (?, ?, ?, 'r', 'pending', 'create_issue', 'acme-co/web', ?, ?, ?)",
            (PID, CH, THREAD, canonical_json(draft), START.isoformat(), START.isoformat()),
        )
        for user, field in ((ALICE, "repo"), (BOB, "assign"), (ALICE, "unassign")):
            db.execute(
                "INSERT INTO proposal_edits (proposal_id, edited_by, field, edited_at)"
                " VALUES (?, ?, ?, ?)",
                (PID, user, field, START.isoformat()),
            )
        refused = db.execute("SELECT sql FROM sqlite_master WHERE name = 'proposal_edits'")
        assert "'boards'" not in refused.fetchone()[0]
    new = await SqliteStore.open(path)
    try:
        before = [(e.edited_by, e.field) for e in await new.proposal_edits(PID)]
        assert before == [(ALICE, "repo"), (BOB, "assign"), (ALICE, "unassign")]
        boarded = draft.model_copy(update={"boards": [ROADMAP]})
        await new.edit_proposal(PID, boarded, by=BOB, field="boards")
        after = await new.proposal_edits(PID)
        assert [(e.edited_by, e.field) for e in after][-1] == (BOB, "boards")
        record = await new.get_proposal(PID)
        assert record is not None and isinstance(record.proposal, IssueProposal)
        assert record.proposal.boards == [ROADMAP]
    finally:
        await new.close()
    with opened(path) as db:
        index = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'proposal_edits'"
        ).fetchall()
        assert [row[0] for row in index] == ["proposal_edits_proposal"]


async def test_a_board_edit_waits_while_found_boards_are_still_loading() -> None:
    # #376: right after a start the menu can't offer found boards yet, so saving it
    # would silently drop one the draft has. It's refused, and nothing changes.
    from jotflow.orchestrator.card_edits import BOARDS_LOADING

    rig = Rig()
    found = ProposedBoard(board="example-org/14", reason="Its team's")
    await rig.proposal(ROADMAP, found)
    rig.edits._board_catalog.awaiting = lambda channel_id: True  # type: ignore[method-assign]
    await click(rig.app(), menu("roadmap"))
    assert await rig.boards() == [
        ("acme-co/3", "Planned for next quarter"),
        ("example-org/14", "Its team's"),
    ]
    assert rig.slack.ephemerals == [(CH, BOB, BOARDS_LOADING)]
    assert rig.cards.updates == []
