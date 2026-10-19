"""The live card: posted once, edited in place on every change, retried on failure (#152)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import IssueProposal, ProposalRecord, ProposalState
from jotflow.orchestrator.card_sync import (
    FIRST_RETRY,
    LAST_RETRY,
    BoltPeople,
    CardSync,
    card_context,
)
from jotflow.orchestrator.cards import Card, CardPublisher
from jotflow.policy import Policy
from jotflow.store import MemoryStore, SqliteStore, Store
from jotflow.store.hashing import content_hash

CH = "C0IDEAS01"
THREAD = "1760781600.000100"
START = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
INJECTION = "<!channel> <@U0EVIL> <https://evil.example|click> *bold*"
POLICY = Policy.model_validate(
    {"channels": {CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "W"}]}}}
)


class FakeCardPoster:
    """Records cards, and fails the next ``failures`` calls."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, Card]] = []
        self.updates: list[tuple[str, str, Card]] = []
        self.failures = 0

    def _maybe_fail(self) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError(f"ratelimited {INJECTION}")

    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        self._maybe_fail()
        self.posts.append((channel_id, thread_ts, card))
        return f"1760782000.{len(self.posts):06d}"

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        self._maybe_fail()
        self.updates.append((channel_id, ts, card))


class FakePeople:
    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.names = names or {"U0ALICE": "Alice", "U0CAROL": "Carol"}

    async def name(self, user_id: str) -> str | None:
        return self.names.get(user_id)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


def proposal(proposal_id: str = "p1", **fields: Any) -> ProposalRecord:
    draft = IssueProposal(
        repo="acme-co/web",
        title=fields.pop("title", "Progress bar"),
        body="Add one.",
        confidence=0.9,
    )
    return ProposalRecord(
        id=proposal_id,
        channel_id=fields.pop("channel", CH),
        thread_ts=fields.pop("thread_ts", THREAD),
        run_id="draft-run",
        proposal=draft,
        participants=["U0ALICE"],
        created_at=fields.pop("created_at", START),
        **fields,
    )


def sync_for(store: Store, poster: FakeCardPoster, clock: Clock | None = None) -> CardSync:
    return CardSync(
        store, POLICY, CardPublisher(store, poster), FakePeople(), clock=clock or Clock()
    )


def text_of(card: Card) -> str:
    return json.dumps(card.blocks)


async def approve(store: Store, proposal_id: str = "p1", by: str = "U0CAROL") -> None:
    record = await store.get_proposal(proposal_id)
    assert record is not None
    await store.transition(
        proposal_id,
        ProposalState.APPROVED,
        approved_by=by,
        expected_hash=content_hash(record.proposal),
    )


# --- one card, edited in place ------------------------------------------------------


async def test_a_new_proposal_gets_one_card_in_its_thread(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    assert await cards.sync() == 1
    assert await cards.sync() == 0
    ((channel, thread, card),) = poster.posts
    assert (channel, thread) == (CH, THREAD)
    assert "Progress bar" in text_of(card)
    stored = await store.get_proposal("p1")
    assert stored is not None and stored.card_ts == "1760782000.000001"
    assert await store.cards_due() == []


async def test_every_later_state_edits_the_same_card(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    await cards.sync()
    await approve(store)
    await cards.sync()
    await store.transition("p1", ProposalState.EXECUTING)
    await cards.sync()
    assert len(poster.posts) == 1
    assert [ts for _, ts, _ in poster.updates] == ["1760782000.000001"] * 2
    assert "Approved by <@U0CAROL>" in text_of(poster.updates[0][2])
    assert "Creating it on GitHub now." in text_of(poster.updates[1][2])


async def test_a_superseded_card_says_so(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal("old"))
    await cards.sync()
    await store.add_proposal(proposal("new", title="Progress bar, v2"))
    await store.supersede_pending(CH, THREAD, keep_id="new")
    await cards.sync()
    assert len(poster.posts) == 2
    ((_, ts, card),) = poster.updates
    assert ts == "1760782000.000001"
    assert "Replaced by a newer proposal in this thread." in text_of(card)


async def test_a_proposal_that_ended_before_its_card_gets_none(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal("old"))
    await store.add_proposal(proposal("new"))
    await store.supersede_pending(CH, THREAD, keep_id="new")
    await cards.sync()
    assert [p[2].text for p in poster.posts] == ["Proposed issue for acme-co/web"]
    assert await store.cards_due() == []


async def test_an_auto_approved_proposal_is_posted_approved(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    await approve(store, by="auto")
    await cards.sync()
    ((_, _, card),) = poster.posts
    assert "Approved by auto mode" in text_of(card)
    assert "jotflow_approve" not in text_of(card)


async def test_a_change_while_drawing_is_drawn_again(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    real_post = poster.post

    async def approve_mid_post(channel_id: str, thread_ts: str, card: Card) -> str:
        ts = await real_post(channel_id, thread_ts, card)
        await approve(store)  # lands after the pending card was drawn
        return ts

    poster.post = approve_mid_post  # type: ignore[method-assign]
    await cards.sync()
    assert [p.id for p in await store.cards_due()] == ["p1"]
    await cards.sync()
    assert "Approved by <@U0CAROL>" in text_of(poster.updates[0][2])


# --- never in the write path; failures are retried ---------------------------------


async def test_a_slack_failure_leaves_the_proposal_and_is_retried(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    poster, clock = FakeCardPoster(), Clock()
    cards = sync_for(store, poster, clock)
    await store.add_proposal(proposal())
    poster.failures = 2
    assert await cards.sync() == 0
    stored = await store.get_proposal("p1")
    assert stored is not None and stored.state is ProposalState.PENDING and stored.card_ts is None
    assert await cards.sync() == 0  # still waiting: no retry before the wait is over
    assert poster.failures == 1
    clock.now += FIRST_RETRY
    assert await cards.sync() == 0  # the second failure
    clock.now += FIRST_RETRY
    assert await cards.sync() == 0  # the wait doubled
    clock.now += FIRST_RETRY
    assert await cards.sync() == 1
    assert len(poster.posts) == 1
    assert "RuntimeError" in caplog.text and "evil.example" not in caplog.text


async def test_the_wait_between_retries_stops_growing(store: Store) -> None:
    poster, clock = FakeCardPoster(), Clock()
    cards = sync_for(store, poster, clock)
    await store.add_proposal(proposal())
    poster.failures = 100
    for _ in range(12):
        await cards.sync()
        clock.now += LAST_RETRY
    tries = 100 - poster.failures
    assert tries == 12


async def test_refresh_draws_one_proposal_now(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    assert await cards.refresh("p1")
    assert not await cards.refresh("p1")  # nothing changed since
    assert not await cards.refresh("nope")
    assert len(poster.posts) == 1


async def test_a_proposal_in_a_channel_no_longer_configured_is_skipped(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal(channel="C0GONE01"))
    assert await cards.sync() == 0
    assert poster.posts == []
    assert await store.cards_due() == []
    assert "not configured" in caplog.text


async def test_agent_text_on_a_live_card_is_never_mrkdwn(store: Store) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal(title=INJECTION))
    await cards.sync()
    ((_, _, card),) = poster.posts
    for block in card.blocks:
        for value in json.dumps(block).split('"type": "mrkdwn"')[1:]:
            assert "<!channel>" not in value.split("}")[0]
    assert INJECTION not in card.text


# --- what the card knows ---------------------------------------------------------------


async def test_the_context_names_participants_and_the_approver(store: Store) -> None:
    await store.link_identity("U0ALICE", "acme-alice")
    await store.add_proposal(proposal())
    await approve(store)
    record = await store.get_proposal("p1")
    assert record is not None
    context = await card_context(POLICY, store, FakePeople(), record)
    assert context.names == {"U0ALICE": "Alice", "U0CAROL": "Carol"}
    assert set(context.identities) == {"U0ALICE"}
    assert context.channel == POLICY.channel(CH)


async def test_auto_is_not_looked_up_as_a_person(store: Store) -> None:
    asked: list[str] = []

    class Recording(FakePeople):
        async def name(self, user_id: str) -> str | None:
            asked.append(user_id)
            return None

    await store.add_proposal(proposal())
    await approve(store, by="auto")
    record = await store.get_proposal("p1")
    assert record is not None
    await card_context(POLICY, store, Recording(), record)
    assert asked == ["U0ALICE"]


class FakeUsers:
    def __init__(self, user: dict[str, Any] | Exception) -> None:
        self.user = user
        self.calls = 0

    async def users_info(self, *, user: str) -> dict[str, Any]:
        self.calls += 1
        if isinstance(self.user, Exception):
            raise self.user
        return {"ok": True, "user": self.user}


@pytest.mark.parametrize(
    ("user", "name"),
    [
        ({"profile": {"display_name": "Alex", "real_name": "Alex Smith"}}, "Alex"),
        ({"profile": {"display_name": " ", "real_name": "Alex Smith"}}, "Alex Smith"),
        ({"name": "alex"}, "alex"),
        ({}, None),
    ],
)
async def test_people_reads_the_best_name_once(user: dict[str, Any], name: str | None) -> None:
    client = FakeUsers(user)
    people = BoltPeople(client)
    assert await people.name("U0ALEX") == name
    assert await people.name("U0ALEX") == name
    assert client.calls == 1


async def test_people_survives_a_failed_lookup(caplog: pytest.LogCaptureFixture) -> None:
    people = BoltPeople(FakeUsers(RuntimeError("user_not_found")))
    assert await people.name("U0ALEX") is None
    assert "RuntimeError" in caplog.text


def test_retry_waits_are_short_then_capped() -> None:
    assert FIRST_RETRY < LAST_RETRY <= timedelta(minutes=1)


# --- review: final errors, crowding, and one draw at a time --------------------------


class SlackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


@pytest.mark.parametrize("code", ["message_not_found", "channel_not_found", "is_archived"])
async def test_a_card_slack_cannot_ever_draw_is_left_alone(
    store: Store, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    await cards.sync()
    await approve(store)

    async def gone(channel_id: str, ts: str, card: Card) -> None:
        raise SlackError(code)

    poster.update = gone  # type: ignore[method-assign]
    assert await cards.sync() == 0
    assert await store.cards_due() == []
    assert code in caplog.text


async def test_proposals_waiting_to_retry_do_not_crowd_out_others(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow.orchestrator import card_sync

    monkeypatch.setattr(card_sync, "BATCH", 2)
    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    for n in (1, 2):
        created = START + timedelta(minutes=n)
        await store.add_proposal(
            proposal(f"p{n}", thread_ts=f"17607816{n:02d}.000100", created_at=created)
        )
    poster.failures = 2
    await cards.sync()  # both fail and wait
    await store.add_proposal(
        proposal("p3", thread_ts="1760781603.000100", created_at=START + timedelta(minutes=3))
    )
    assert await cards.sync() == 1
    assert [p[1] for p in poster.posts] == ["1760781603.000100"]


async def test_sync_and_refresh_never_post_the_same_card_twice(store: Store) -> None:
    import asyncio

    poster = FakeCardPoster()
    cards = sync_for(store, poster)
    await store.add_proposal(proposal())
    real_post = poster.post

    async def slow(channel_id: str, thread_ts: str, card: Card) -> str:
        await asyncio.sleep(0.01)
        return await real_post(channel_id, thread_ts, card)

    poster.post = slow  # type: ignore[method-assign]
    await asyncio.gather(cards.sync(), cards.refresh("p1"))
    assert len(poster.posts) == 1
