"""The give-up notice (#30)."""

from __future__ import annotations

from typing import Any

from jotflow.models import ProposalState
from jotflow.orchestrator.cards import FAILED_LINE, Card, CardPublisher
from jotflow.orchestrator.notices import GAVE_UP_NOTICE, GiveUpNotice
from jotflow.store import MemoryStore
from tests.test_runs import CH, POLICY, THREAD, _approved

SECRET_REASON = "private repo said: SECRET-REASON"


class Poster:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        self.posts.append((channel_id, thread_ts, text))
        return "2.0"


class Cards:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str, Card]] = []

    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        raise AssertionError("a failed proposal's card is edited, never posted anew")

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        self.updates.append((channel_id, ts, card))


async def _failed(store: MemoryStore, *, card_ts: str | None) -> Any:
    await _approved(store)
    await store.transition("p1", ProposalState.EXECUTING)
    await store.transition("p1", ProposalState.RECONCILING)
    record = await store.transition("p1", ProposalState.FAILED)
    if card_ts is not None:
        record = await store.set_card_ts("p1", card_ts)
    return record


async def test_without_a_card_one_fixed_reply_goes_to_the_thread() -> None:
    store = MemoryStore()
    record = await _failed(store, card_ts=None)
    poster, cards = Poster(), Cards()
    await GiveUpNotice(poster, POLICY, cards=CardPublisher(store, cards))(record)
    assert poster.posts == [(CH, THREAD, GAVE_UP_NOTICE.format(attempts=3))]
    assert cards.updates == []


async def test_with_a_card_the_card_shows_the_fixed_failed_line() -> None:
    store = MemoryStore()
    record = await _failed(store, card_ts="1760781699.000500")
    poster, cards = Poster(), Cards()
    await GiveUpNotice(poster, POLICY, cards=CardPublisher(store, cards))(record)
    assert poster.posts == []
    ((channel, ts, card),) = cards.updates
    assert (channel, ts) == (CH, "1760781699.000500")
    assert FAILED_LINE in str(card.blocks)
    assert "SECRET-REASON" not in str(card.blocks) and "SECRET-REASON" not in card.text


async def test_without_a_card_publisher_the_thread_is_told() -> None:
    store = MemoryStore()
    record = await _failed(store, card_ts="1760781699.000500")
    poster = Poster()
    await GiveUpNotice(poster, POLICY, attempts=5)(record)
    assert poster.posts == [(CH, THREAD, GAVE_UP_NOTICE.format(attempts=5))]
