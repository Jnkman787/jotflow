"""The card shows who the issue will credit, and the footer credits no one else (#396)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jotflow.execution.attribution import execution_footer
from jotflow.models import IssueProposal, MessageRef, ProposalRecord, ProposalState
from jotflow.orchestrator.card_sync import CardSync
from jotflow.orchestrator.cards import CardPublisher
from jotflow.store import MemoryStore, SqliteStore, Store, content_hash
from jotflow.store.base import AUTO_APPROVER
from tests.test_card_sync import FakeCardPoster, FakePeople
from tests.test_execution_attribution import CH, KIM, REPLY, ROOT, Crowd, policy

ADDED = "1792310900.000100"  # a reply linked after the card was drawn


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


class Later(Crowd):
    """Slack, with Kim's later reply and the person who wrote it."""

    def __init__(self) -> None:
        super().__init__()
        self._authors[ADDED] = {"U0LATE"}  # type: ignore[index]

    async def name(self, user_id: str) -> str | None:
        return "Latecomer" if user_id == "U0LATE" else await super().name(user_id)


async def drafted(store: Store) -> tuple[ProposalRecord, CardSync, FakeCardPoster]:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    for ts, thread in ((ROOT, None), (REPLY, ROOT), (KIM, ROOT)):
        await store.link_message(MessageRef(channel_id=CH, ts=ts, thread_ts=thread), topic.id)
    record = await store.add_proposal(
        ProposalRecord(
            id="0123456789abcdef",
            channel_id=CH,
            thread_ts=ROOT,
            run_id="r1",
            proposal=IssueProposal(
                topic_id=topic.id,
                repo="acme-co/web",
                title="t",
                body="b",
                confidence=0.9,
                sources=[MessageRef(channel_id=CH, ts=ROOT)],
            ),
            participants=["U0SAM"],
            created_at=datetime(2026, 10, 19, tzinfo=UTC),
        )
    )
    poster = FakeCardPoster()
    sync = CardSync(
        store, policy(), CardPublisher(store, poster), FakePeople(), attribution=Later()
    )
    assert await sync.sync() == 1
    return record, sync, poster


def text(poster: FakeCardPoster) -> str:
    card = (poster.updates or poster.posts)[-1][2]
    return str(card.blocks)


async def test_a_drawn_card_shows_the_credit_line_and_locks_its_ids(store: Store) -> None:
    record, _, poster = await drafted(store)
    assert "Credited in the issue: raised by Sam, with contributions from Alex" in text(poster)
    shown = await store.get_proposal(record.id)
    assert shown is not None and shown.card_credits is not None
    assert set(json.loads(shown.card_credits)) == {"U0SAM", "U0ALEX", "U0KIM"}


async def approve(store: Store, record: ProposalRecord) -> ProposalRecord:
    await store.transition(
        record.id,
        ProposalState.APPROVED,
        approved_by="U0BOB",
        expected_hash=content_hash(record.proposal),
    )
    approved = await store.get_proposal(record.id)
    assert approved is not None
    return approved


async def test_someone_linked_after_the_card_was_drawn_isnt_credited(store: Store) -> None:
    record, _, _ = await drafted(store)
    topic_id = record.proposal.topic_id
    assert topic_id is not None
    reply = MessageRef(channel_id=CH, ts=ADDED, thread_ts=ROOT)
    await store.link_message(reply, topic_id)
    approved = await approve(store, record)
    tail = await execution_footer(approved, policy(), Later(), store)
    assert "Sam" in tail.splitlines()[0] and "Latecomer" not in tail
    # A redraw after approval doesn't widen what the card locked.
    await store.mark_card_shown(approved.id, approved.revision, None, json.dumps(["U0LATE"]))
    again = await store.get_proposal(record.id)
    assert again is not None and again.card_credits == approved.card_credits


async def test_a_card_that_credited_no_one_names_no_one(store: Store) -> None:
    record, _, _ = await drafted(store)
    await store.mark_card_shown(record.id, record.revision, None, "[]")
    approved = await approve(store, record)
    tail = await execution_footer(approved, policy(), Later(), store)
    assert not any(name in tail for name in ("Sam", "Alex", "Kim"))


async def test_an_auto_approved_proposal_credits_by_the_footers_rules(store: Store) -> None:
    record, _, _ = await drafted(store)
    await store.transition(
        record.id,
        ProposalState.APPROVED,
        approved_by=AUTO_APPROVER,
        expected_hash=content_hash(record.proposal),
    )
    auto = await store.get_proposal(record.id)
    assert auto is not None
    reply = MessageRef(channel_id=CH, ts=ADDED, thread_ts=ROOT)
    assert record.proposal.topic_id is not None
    await store.link_message(reply, record.proposal.topic_id)
    tail = await execution_footer(auto, policy(), Later(), store)
    assert "Sam" in tail  # no person approved from a card, so nothing narrows it


def test_a_record_without_shown_credits_credits_no_one() -> None:
    from jotflow.execution.attribution import shown_credits

    base: Any = ProposalRecord(
        id="0123456789abcdef",
        channel_id=CH,
        thread_ts=ROOT,
        run_id="r1",
        proposal=IssueProposal(repo="acme-co/web", title="t", body="b", confidence=0.9),
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    assert shown_credits(base) == frozenset()
    assert shown_credits(base.model_copy(update={"card_credits": "not json"})) == frozenset()
    assert shown_credits(base.model_copy(update={"card_credits": '["U0SAM", 3]'})) == {"U0SAM"}
    assert shown_credits(base.model_copy(update={"approved_by": AUTO_APPROVER})) is None


async def test_a_store_before_migration_24_records_and_reads_no_credits(tmp_path: Path) -> None:
    """Upgrade tests open older schemas: card_credits is read and written only when
    the column exists, and a store without it credits no one."""
    from jotflow.store.migrations import MIGRATIONS

    old = await SqliteStore.open(tmp_path / "old.db", migrations=MIGRATIONS[:23])
    try:
        record = await old.add_proposal(
            ProposalRecord(
                id="0123456789abcdef",
                channel_id=CH,
                thread_ts=ROOT,
                run_id="r1",
                proposal=IssueProposal(repo="acme-co/web", title="t", body="b", confidence=0.9),
                created_at=datetime(2026, 10, 19, tzinfo=UTC),
            )
        )
        await old.mark_card_shown(record.id, record.revision, '{"label": null}', '["U0SAM"]')
        found = await old.get_proposal(record.id)
        assert found is not None and found.card_credits is None
        assert found.card_issues == '{"label": null}' and found.card_revision == record.revision
    finally:
        await old.close()
    new = await SqliteStore.open(tmp_path / "old.db")
    try:
        await new.mark_card_shown("0123456789abcdef", 1, None, '["U0SAM"]')
        found = await new.get_proposal("0123456789abcdef")
        assert found is not None and found.card_credits == '["U0SAM"]'
    finally:
        await new.close()
