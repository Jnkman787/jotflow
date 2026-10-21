"""Related issues on the card, "same as", and its undo (#36)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import (
    IssueProposal,
    ProposalRecord,
    ProposalState,
    RelatedIssue,
    RunKind,
    UpdateProposal,
)
from jotflow.orchestrator.cards import (
    ACTION_NOT_DUPLICATE,
    ACTION_SAME_AS,
    CardContext,
    render_card,
)
from jotflow.policy import Policy
from jotflow.store import MemoryStore, SqliteStore, Store, content_hash
from jotflow.store.base import (
    DUPLICATE_MOVED_ON,
    DUPLICATE_NO_CANDIDATE,
    DUPLICATE_NOT_NEW,
    DUPLICATE_NOT_PENDING,
    DUPLICATE_TOPIC_LINKED,
    DUPLICATE_UNDONE,
    ContentChangedError,
    DuplicateRefusedError,
)
from jotflow.store.hashing import canonical_json
from jotflow.store.migrations import MIGRATIONS
from tests.sqlite_db import opened
from tests.test_approval import ALICE, BOB, CARD_TS, Rig, click
from tests.test_runs import CH, START, THREAD

PID = "0123456789abcdef"
WEB42 = {"repo": "acme-co/web", "number": 42, "state": "closed"}
WEB57 = {"repo": "acme-co/web", "number": 57, "state": "open"}
POLICY = Policy.model_validate(
    {"channels": {CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "W"}]}}}
)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    if request.param == "memory":
        yield MemoryStore()
        return
    opened = await SqliteStore.open(tmp_path / "jotflow.db")
    try:
        yield opened
    finally:
        await opened.close()


async def pending(store: Store, *, related: list[dict[str, Any]] | None = None) -> ProposalRecord:
    topic = await store.create_topic(CH, "Progress bar")
    draft = IssueProposal(
        topic_id=topic.topic.id,
        repo="acme-co/web",
        title="Progress bar",
        body="Add one.",
        confidence=0.9,
        related=[WEB42, WEB57] if related is None else related,
    )
    record = ProposalRecord(
        id=PID,
        channel_id=CH,
        thread_ts=THREAD,
        run_id="draft-run",
        proposal=draft,
        created_at=START,
    )
    return await store.add_proposal(record)


# --- the store: mark and undo ------------------------------------------------------------


async def test_same_as_closes_the_proposal_binds_the_topic_and_clears_the_draft(
    store: Store,
) -> None:
    record = await pending(store)
    marked = await store.mark_duplicate(
        PID, index=0, expected_hash=content_hash(record.proposal), by=BOB
    )
    assert marked.state is ProposalState.REJECTED
    assert marked.duplicate is not None
    assert (marked.duplicate.repo, marked.duplicate.number, marked.duplicate.by) == (
        "acme-co/web",
        42,
        BOB,
    )
    assert marked.draft_cleared_at is not None and marked.proposal.body == ""
    assert marked.revision > record.revision
    topic = await store.get_topic(record.proposal.topic_id or "")
    assert topic is not None and (topic.topic.repo, topic.topic.issue_number) == ("acme-co/web", 42)
    # Read back from the store, the record is the same.
    again = await store.get_proposal(PID)
    assert again is not None and again.duplicate == marked.duplicate


async def test_the_issue_comes_from_the_stored_draft_by_index(store: Store) -> None:
    record = await pending(store)
    marked = await store.mark_duplicate(
        PID, index=1, expected_hash=content_hash(record.proposal), by=BOB
    )
    assert marked.duplicate is not None and marked.duplicate.number == 57


@pytest.mark.parametrize(
    ("setup", "error", "reason"),
    [
        ("stale", ContentChangedError, None),
        ("index", DuplicateRefusedError, DUPLICATE_NO_CANDIDATE),
        ("linked", DuplicateRefusedError, DUPLICATE_TOPIC_LINKED),
        ("approved", DuplicateRefusedError, DUPLICATE_NOT_PENDING),
    ],
)
async def test_a_same_as_that_cant_apply_changes_nothing(
    store: Store, setup: str, error: type[Exception], reason: str | None
) -> None:
    record = await pending(store)
    hashed = content_hash(record.proposal)
    index = 0
    if setup == "stale":
        hashed = "sha256:" + "0" * 64
    elif setup == "index":
        index = 2
    elif setup == "linked":
        await store.set_topic_issue(record.proposal.topic_id or "", "acme-co/web", 9)
    elif setup == "approved":
        await store.transition(PID, ProposalState.APPROVED, approved_by=ALICE, expected_hash=hashed)
    with pytest.raises(error) as raised:
        await store.mark_duplicate(PID, index=index, expected_hash=hashed, by=BOB)
    if reason is not None:
        assert str(raised.value) == reason
    after = await store.get_proposal(PID)
    assert after is not None and after.duplicate is None
    assert after.state is (ProposalState.APPROVED if setup == "approved" else ProposalState.PENDING)


async def test_an_update_proposal_cant_be_marked_the_same(store: Store) -> None:
    topic = await store.create_topic(CH, "T")
    await store.set_topic_issue(topic.topic.id, "acme-co/web", 3)
    draft = UpdateProposal(
        topic_id=topic.topic.id,
        repo="acme-co/web",
        issue_number=3,
        changes={"comment": "c"},
        confidence=0.9,
        related=[WEB42],
    )
    record = await store.add_proposal(
        ProposalRecord(
            id=PID, channel_id=CH, thread_ts=THREAD, run_id="r", proposal=draft, created_at=START
        )
    )
    with pytest.raises(DuplicateRefusedError, match=DUPLICATE_NOT_NEW):
        await store.mark_duplicate(
            PID, index=0, expected_hash=content_hash(record.proposal), by=BOB
        )


async def test_undo_unbinds_the_topic_and_records_who(store: Store) -> None:
    record = await pending(store)
    await store.mark_duplicate(PID, index=0, expected_hash=content_hash(record.proposal), by=BOB)
    undone = await store.undo_duplicate(PID, by=ALICE)
    assert undone.duplicate is not None
    assert undone.duplicate.undone_by == ALICE and undone.duplicate.undone_at is not None
    assert undone.duplicate.by == BOB  # the record of the mark stays
    assert undone.state is ProposalState.REJECTED
    topic = await store.get_topic(record.proposal.topic_id or "")
    assert topic is not None and topic.topic.issue_number is None
    with pytest.raises(DuplicateRefusedError, match=DUPLICATE_UNDONE):
        await store.undo_duplicate(PID, by=ALICE)


async def test_undo_refuses_once_the_topic_has_moved_on(store: Store) -> None:
    record = await pending(store)
    await store.mark_duplicate(PID, index=0, expected_hash=content_hash(record.proposal), by=BOB)
    await store.set_topic_issue(record.proposal.topic_id or "", "acme-co/web", 99)
    with pytest.raises(DuplicateRefusedError, match="moved on"):
        await store.undo_duplicate(PID, by=ALICE)
    assert DUPLICATE_MOVED_ON.startswith("This conversation has moved on")


async def test_a_plain_rejection_has_no_duplicate_and_cant_be_undone(store: Store) -> None:
    await pending(store)
    rejected = await store.transition(PID, ProposalState.REJECTED)
    assert rejected.duplicate is None
    with pytest.raises(DuplicateRefusedError):
        await store.undo_duplicate(PID, by=ALICE)


# --- the hash and the schema ---------------------------------------------------------------


def test_a_draft_without_related_hashes_as_before_the_field_existed() -> None:
    draft = IssueProposal(repo="acme-co/web", title="T", body="B", confidence=0.5)
    assert "related" not in canonical_json(draft, hashed=True)
    with_related = draft.model_copy(update={"related": [RelatedIssue(**WEB42)]})
    assert content_hash(with_related) != content_hash(draft)


async def test_migration_14_keeps_existing_proposals(tmp_path: Path) -> None:
    path = tmp_path / "jotflow.db"
    old = await SqliteStore.open(path, migrations=MIGRATIONS[:13])
    await old.close()
    draft = IssueProposal(repo="acme-co/web", title="T", body="B", confidence=0.5)
    with opened(path) as db:
        db.execute(
            "INSERT INTO proposals (id, channel_id, thread_ts, run_id, state, kind, repo,"
            " draft_json, created_at, updated_at)"
            " VALUES (?, ?, ?, 'r', 'pending', 'create_issue', 'acme-co/web', ?, ?, ?)",
            (PID, CH, THREAD, canonical_json(draft), START.isoformat(), START.isoformat()),
        )
    new = await SqliteStore.open(path)
    try:
        record = await new.get_proposal(PID)
        assert record is not None and record.duplicate is None
        assert record.state is ProposalState.PENDING
        assert content_hash(record.proposal) == content_hash(draft)
    finally:
        await new.close()


# --- the card -------------------------------------------------------------------------------


async def test_the_card_shows_related_as_the_agents_claim_and_offers_same_as() -> None:
    store = MemoryStore()
    record = await pending(store)
    card = render_card(record, CardContext(channel=POLICY.channel(CH)))
    text = str(card.blocks)
    assert (
        "Related: <https://github.com/acme-co/web/issues/42|acme-co/web#42> (agent says closed)"
        in text
    )
    assert "(agent says open)" in text
    buttons = [e for b in card.blocks if b["type"] == "actions" for e in b["elements"]]
    same = [b for b in buttons if b["action_id"] == ACTION_SAME_AS]
    assert len(same) == 1
    assert same[0]["text"]["text"] == "Same as acme-co/web#42"
    assert same[0]["value"] == f"{PID}:{content_hash(record.proposal)}:0"


async def test_no_same_as_without_related() -> None:
    store = MemoryStore()
    record = await pending(store, related=[])
    card = render_card(record, CardContext(channel=POLICY.channel(CH)))
    assert ACTION_SAME_AS not in str(card.blocks) and "Related:" not in str(card.blocks)


async def test_a_same_as_card_says_who_and_offers_the_undo_until_it_is_used() -> None:
    store = MemoryStore()
    record = await pending(store)
    marked = await store.mark_duplicate(
        PID, index=0, expected_hash=content_hash(record.proposal), by=BOB
    )
    text = str(render_card(marked, CardContext(channel=POLICY.channel(CH))).blocks)
    assert (
        "Same as <https://github.com/acme-co/web/issues/42|acme-co/web#42>, marked by <@U0BOB>"
        in text
    )
    assert "Rejected. Nothing was written." not in text
    assert ACTION_NOT_DUPLICATE in text
    undone = await store.undo_duplicate(PID, by=ALICE)
    text = str(render_card(undone, CardContext(channel=POLICY.channel(CH))).blocks)
    assert "undone by <@U0ALICE>" in text and ACTION_NOT_DUPLICATE not in text


# --- the gate ------------------------------------------------------------------------------


async def rigged(rig: Rig) -> str:
    record = await pending(rig.store)
    await rig.store.set_card_ts(record.id, CARD_TS)
    return record.id


def same_as(value: str, **kwargs: Any) -> dict[str, Any]:
    body = click(value, **kwargs)
    body["actions"][0]["action_id"] = ACTION_SAME_AS
    return body


async def test_a_same_as_click_marks_it_and_redraws_the_card() -> None:
    rig = Rig()
    pid = await rigged(rig)
    assert await rig.handler.on_same_as(same_as(f"{pid}:{await rig.hash(pid)}:0")) == "same_as"
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.duplicate is not None and record.duplicate.by == BOB
    assert rig.cards.updates and "Same as" in str(rig.cards.updates[-1][2].blocks)
    assert await rig.executions() == 0  # no GitHub write, ever


async def test_same_as_follows_approves_rules_for_who_may_click() -> None:
    rig = Rig(approvers=[ALICE])
    pid = await rigged(rig)
    value = f"{pid}:{await rig.hash(pid)}:0"
    assert await rig.handler.on_same_as(same_as(value, user=BOB)) == "refused"
    assert await rig.state(pid) is ProposalState.PENDING
    assert await rig.handler.on_same_as(same_as(value, user=ALICE)) == "same_as"


async def test_a_stale_same_as_marks_nothing() -> None:
    rig = Rig()
    pid = await rigged(rig)
    assert await rig.handler.on_same_as(same_as(f"{pid}:sha256:{'1' * 64}:0")) == "refused"
    assert await rig.state(pid) is ProposalState.PENDING
    assert "out of date" in rig.slack.ephemerals[-1][2]


@pytest.mark.parametrize("value", [f"{PID}:0", f"{PID}:sha256:{'0' * 64}", "x:y:z:0"])
async def test_a_malformed_same_as_value_changes_nothing(value: str) -> None:
    rig = Rig()
    pid = await rigged(rig)
    assert await rig.handler.on_same_as(same_as(value)) == "refused"
    assert await rig.state(pid) is ProposalState.PENDING


async def test_not_a_duplicate_unbinds_and_drafts_the_thread_again() -> None:
    rig = Rig()
    pid = await rigged(rig)
    await rig.handler.on_same_as(same_as(f"{pid}:{await rig.hash(pid)}:0"))
    undo = click(pid, user=ALICE)
    undo["actions"][0]["action_id"] = ACTION_NOT_DUPLICATE
    assert await rig.handler.on_not_duplicate(undo) == "not_duplicate"
    runs = await rig.store.thread_runs(CH, THREAD)
    assert [r.kind for r in runs] == [RunKind.DRAFTING]
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.duplicate is not None
    assert record.duplicate.undone_by == ALICE


# --- card sync: a seen card is edited into its final state -----------------------------------


async def test_card_sync_edits_a_same_as_card_it_already_posted(store: Store) -> None:
    from jotflow.orchestrator.card_sync import CardSync
    from jotflow.orchestrator.cards import CardPublisher
    from tests.test_card_sync import Clock, FakeCardPoster, FakePeople

    poster = FakeCardPoster()
    sync = CardSync(store, POLICY, CardPublisher(store, poster), FakePeople(), clock=Clock())
    record = await pending(store)
    assert await sync.sync() == 1 and len(poster.posts) == 1
    await store.mark_duplicate(PID, index=0, expected_hash=content_hash(record.proposal), by=BOB)
    # REJECTED is in _NO_CARD_NEEDED, but only for a proposal nobody saw.
    assert await sync.sync() == 1
    ((_, _, card),) = poster.updates
    assert "Same as" in str(card.blocks) and ACTION_NOT_DUPLICATE in str(card.blocks)


async def test_undo_refuses_while_a_later_proposal_relies_on_the_binding(store: Store) -> None:
    record = await pending(store)
    await store.mark_duplicate(PID, index=0, expected_hash=content_hash(record.proposal), by=BOB)
    # A follow-up in the thread became an update to the bound issue, and was approved.
    topic_id = record.proposal.topic_id or ""
    update = UpdateProposal(
        topic_id=topic_id,
        repo="acme-co/web",
        issue_number=42,
        changes={"comment": "also"},
        confidence=0.9,
    )
    later = await store.add_proposal(
        ProposalRecord(
            id="fedcba9876543210",
            channel_id=CH,
            thread_ts=THREAD,
            run_id="r2",
            proposal=update,
            created_at=START.replace(year=2030),
        )
    )
    await store.transition(
        later.id,
        ProposalState.APPROVED,
        approved_by=ALICE,
        expected_hash=content_hash(later.proposal),
    )
    with pytest.raises(DuplicateRefusedError, match="moved on"):
        await store.undo_duplicate(PID, by=ALICE)
    topic = await store.get_topic(topic_id)
    assert topic is not None and topic.topic.issue_number == 42
