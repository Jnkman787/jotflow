from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jotflow.models import MessageRef, RunKind, RunStatus
from jotflow.orchestrator.debounce import Debouncer, Thread, thread_of
from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter, Ignored
from jotflow.policy import Policy
from jotflow.store import MemoryStore, SqliteStore, Store

CH = "C0IDEAS01"
FAST = "C0IDEAS02"
ROOT = "1760781600.000100"
START = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
POLICY = Policy.model_validate(
    {
        "debounce_seconds": 120,
        "channels": {
            CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]},
            FAST: {
                "debounce_seconds": 10,
                "repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}],
            },
        },
    }
)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory", "sqlite"])
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, clock: Clock
) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore(clock=clock)
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)
    )
    yield made
    await made.close()


@pytest.fixture
def debouncer(store: Store, clock: Clock) -> Debouncer:
    return Debouncer(store, POLICY, clock=clock)


def message(ts: str, thread: str | None = None, channel: str = CH) -> Accepted:
    return Accepted("message", MessageRef(channel_id=channel, ts=ts, thread_ts=thread))


async def queued(store: Store, channel: str, thread: str) -> list[RunKind]:
    return [
        r.kind for r in await store.thread_runs(channel, thread) if r.status is RunStatus.QUEUED
    ]


def test_a_top_level_post_is_its_own_thread() -> None:
    assert thread_of(MessageRef(channel_id=CH, ts=ROOT)) == Thread(CH, ROOT)
    reply = MessageRef(channel_id=CH, ts="1760781601.000200", thread_ts=ROOT)
    assert thread_of(reply) == Thread(CH, ROOT)


async def test_a_burst_of_replies_yields_one_run(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    await debouncer.note(message(ROOT))
    for i in range(5):
        clock.advance(60)  # each reply lands inside the window and pushes it back
        await debouncer.note(message(f"17607816{i:02d}.000200", thread=ROOT))
        assert await debouncer.flush() == []
    clock.advance(119)
    assert await debouncer.flush() == []
    clock.advance(1)
    assert await debouncer.flush() == [Thread(CH, ROOT)]
    assert await queued(store, CH, ROOT) == [RunKind.DRAFTING]
    assert debouncer.pending() == {}
    assert await debouncer.flush() == []


async def test_loose_top_level_posts_each_get_their_own_run(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    await debouncer.note(message("1760781600.000100"))
    clock.advance(30)
    await debouncer.note(message("1760781630.000100"))
    clock.advance(100)
    assert await debouncer.flush() == [Thread(CH, "1760781600.000100")]
    clock.advance(30)
    assert await debouncer.flush() == [Thread(CH, "1760781630.000100")]
    for root in ("1760781600.000100", "1760781630.000100"):
        assert await queued(store, CH, root) == [RunKind.DRAFTING]


async def test_the_window_is_per_channel(store: Store, debouncer: Debouncer, clock: Clock) -> None:
    await debouncer.note(message(ROOT))
    await debouncer.note(message(ROOT, channel=FAST))
    assert debouncer.next_due() == START + timedelta(seconds=10)
    clock.advance(10)
    assert await debouncer.flush() == [Thread(FAST, ROOT)]
    assert debouncer.next_due() == START + timedelta(seconds=120)
    clock.advance(110)
    assert await debouncer.flush() == [Thread(CH, ROOT)]
    assert debouncer.next_due() is None


async def test_threads_flush_in_due_order(store: Store, debouncer: Debouncer, clock: Clock) -> None:
    await debouncer.note(message("1760781600.000300"))
    clock.advance(1)
    await debouncer.note(message("1760781600.000100"))
    clock.advance(500)
    assert [t.thread_ts for t in await debouncer.flush()] == [
        "1760781600.000300",
        "1760781600.000100",
    ]


async def test_a_burst_during_a_running_run_queues_one_follow_up(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    await debouncer.note(message(ROOT))
    clock.advance(120)
    await debouncer.flush()
    running = await store.claim_next_run(max_running=2)
    assert running is not None
    for i in range(3):
        await debouncer.note(message(f"1760781700.00010{i}", thread=ROOT))
        clock.advance(120)
        await debouncer.flush()
    assert await queued(store, CH, ROOT) == [RunKind.DRAFTING]


async def test_only_accepted_messages_count(store: Store, debouncer: Debouncer) -> None:
    assert await debouncer.note(Ignored("own message", CH)) is None
    reaction = Accepted("reaction", MessageRef(channel_id=CH, ts=ROOT), reaction="bug")
    assert await debouncer.note(reaction) is None
    assert debouncer.pending() == {}


async def test_the_bots_own_messages_never_rearm_a_thread(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    """Through the real ingest filter: the bot's reply is dropped before the debouncer."""
    events = EventFilter(POLICY.channels, BotIdentity("U0JOTBOT", "B0JOTBOT"))
    await debouncer.note(
        events.classify({"type": "message", "channel": CH, "ts": ROOT, "user": "U0ALICE"})
    )
    due = debouncer.pending()[Thread(CH, ROOT)]
    clock.advance(100)
    bot_reply = {
        "type": "message",
        "channel": CH,
        "ts": "1760781700.000100",
        "thread_ts": ROOT,
        "user": "U0JOTBOT",
        "bot_id": "B0JOTBOT",
    }
    assert await debouncer.note(events.classify(bot_reply)) is None
    assert debouncer.pending()[Thread(CH, ROOT)] == due


async def test_a_reply_revives_its_threads_expired_topic(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    await store.link_message(MessageRef(channel_id=CH, ts=ROOT), topic.id)
    clock.advance(30 * 24 * 3600)  # well past any topic window
    assert await store.channel_topics(CH, active_since=clock.now - timedelta(days=14)) == []

    reply = message("1763373600.000200", thread=ROOT)
    await debouncer.note(reply)
    link = await store.get_message(CH, reply.ref.ts)
    assert link is not None and (link.topic_id, link.linked_by) == (topic.id, "agent")
    active = await store.channel_topics(CH, active_since=clock.now - timedelta(days=14))
    assert [r.topic.id for r in active] == [topic.id]


async def test_a_reply_already_placed_is_left_alone(store: Store, debouncer: Debouncer) -> None:
    first = (await store.create_topic(CH, "First")).topic
    other = (await store.create_topic(CH, "Other")).topic
    await store.link_message(MessageRef(channel_id=CH, ts=ROOT), first.id)
    reply = MessageRef(channel_id=CH, ts="1760781700.000200", thread_ts=ROOT)
    await store.link_message(reply, other.id, by="person")
    await debouncer.note(Accepted("message", reply))
    link = await store.get_message(CH, reply.ts)
    assert link is not None and link.topic_id == other.id


async def test_a_reply_to_an_unplaced_post_links_nothing(
    store: Store, debouncer: Debouncer
) -> None:
    reply = message("1760781700.000200", thread=ROOT)
    await debouncer.note(reply)
    assert await store.get_message(CH, reply.ref.ts) is None
    await store.mark_chatter(MessageRef(channel_id=CH, ts=ROOT))
    await debouncer.note(message("1760781700.000300", thread=ROOT))
    assert await store.get_message(CH, "1760781700.000300") is None


# A person's skip: never arms the thread (#56)


async def test_a_persons_skip_never_arms_the_thread(debouncer: Debouncer, store: Store) -> None:
    await store.mark_chatter(MessageRef(channel_id=CH, ts=ROOT), by="person")
    assert await debouncer.note(message(ROOT)) is None
    assert await debouncer.note(message("1760781660.000100", thread=ROOT)) is None
    assert debouncer.pending() == {}


async def test_a_skipped_reply_leaves_its_thread_alone(debouncer: Debouncer, store: Store) -> None:
    reply = "1760781660.000100"
    await store.mark_chatter(MessageRef(channel_id=CH, ts=reply, thread_ts=ROOT), by="person")
    assert await debouncer.note(message(reply, thread=ROOT)) is None
    assert debouncer.pending() == {}
    assert await debouncer.note(message("1760781720.000100", thread=ROOT)) is not None


async def test_the_agents_chatter_still_arms_the_thread(debouncer: Debouncer, store: Store) -> None:
    await store.mark_chatter(MessageRef(channel_id=CH, ts=ROOT))
    assert await debouncer.note(message(ROOT)) is not None


# Across a restart (#130)


async def test_a_waiting_thread_is_kept_in_the_store(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    await debouncer.note(message(ROOT))
    clock.advance(30)
    await debouncer.note(message("1760781630.000200", ROOT))
    (pending,) = await store.pending_threads()
    assert (pending.channel_id, pending.thread_ts) == (CH, ROOT)
    assert pending.due_at == clock.now + timedelta(seconds=120)
    clock.advance(121)
    assert await debouncer.flush() == [Thread(CH, ROOT)]
    assert await store.pending_threads() == []


async def test_a_restart_re_arms_the_waiting_thread(store: Store, clock: Clock) -> None:
    before = Debouncer(store, POLICY, clock=clock)
    await before.note(message(ROOT))
    clock.advance(60)
    # JotFlow stops and starts again: a new debouncer with an empty memory.
    after = Debouncer(store, POLICY, clock=clock)
    assert await after.restore() == 1
    assert await after.flush() == []
    clock.advance(61)
    assert await after.flush() == [Thread(CH, ROOT)]
    (run,) = await store.thread_runs(CH, ROOT)
    assert (run.kind, run.status) == (RunKind.DRAFTING, RunStatus.QUEUED)


async def test_a_window_that_ended_while_down_flushes_at_once(store: Store, clock: Clock) -> None:
    await Debouncer(store, POLICY, clock=clock).note(message(ROOT))
    clock.advance(3600)
    after = Debouncer(store, POLICY, clock=clock)
    await after.restore()
    assert await after.flush() == [Thread(CH, ROOT)]


async def test_a_thread_flushed_before_the_restart_is_not_queued_again(
    store: Store, clock: Clock
) -> None:
    before = Debouncer(store, POLICY, clock=clock)
    await before.note(message(ROOT))
    clock.advance(121)
    await before.flush()
    after = Debouncer(store, POLICY, clock=clock)
    assert await after.restore() == 0
    assert await after.flush() == []
    assert len(await store.thread_runs(CH, ROOT)) == 1


async def test_a_root_a_person_skipped_is_not_re_armed(store: Store, clock: Clock) -> None:
    await Debouncer(store, POLICY, clock=clock).note(message(ROOT))
    await store.mark_chatter(MessageRef(channel_id=CH, ts=ROOT), by="person")
    after = Debouncer(store, POLICY, clock=clock)
    assert await after.restore() == 0
    assert await store.pending_threads() == []
    clock.advance(3600)
    assert await after.flush() == []


async def test_a_root_only_the_agent_called_chatter_is_re_armed(store: Store, clock: Clock) -> None:
    # The agent's call is a guess a later message may overturn; a person's is final.
    await Debouncer(store, POLICY, clock=clock).note(message(ROOT))
    await store.mark_chatter(MessageRef(channel_id=CH, ts=ROOT), by="agent")
    assert await Debouncer(store, POLICY, clock=clock).restore() == 1


async def test_a_skip_reaction_on_the_root_inside_the_window_cancels_the_run(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    await debouncer.note(message(ROOT))
    # What the keyword handler stores for a :no_entry_sign: on the first message.
    await store.mark_chatter(MessageRef(channel_id=CH, ts=ROOT), by="person")
    clock.advance(121)
    assert await debouncer.flush() == []
    assert await store.thread_runs(CH, ROOT) == []
    assert await store.pending_threads() == [] and debouncer.pending() == {}


async def test_a_skip_on_a_reply_does_not_cancel_its_thread(
    store: Store, debouncer: Debouncer, clock: Clock
) -> None:
    reply = "1760781630.000200"
    await debouncer.note(message(ROOT))
    await debouncer.note(message(reply, ROOT))
    await store.mark_chatter(MessageRef(channel_id=CH, ts=reply, thread_ts=ROOT), by="person")
    clock.advance(121)
    assert await debouncer.flush() == [Thread(CH, ROOT)]


async def test_a_failed_save_leaves_nothing_waiting_in_memory(
    store: Store, debouncer: Debouncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "arm_thread", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        await debouncer.note(message(ROOT))
    assert debouncer.pending() == {}
