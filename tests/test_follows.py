"""A quick same-author top-level post (#334): the packet's hint, and the debouncer's order."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from jotflow.models import MessageRef
from jotflow.orchestrator.debounce import Debouncer, Thread
from jotflow.orchestrator.ingest import Accepted
from jotflow.orchestrator.packet import FOLLOWS_WITHIN, SlackWorkSource
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore
from tests.test_packet import FakeReader

CH = "C0IDEAS01"
FIRST = "1760781600.000100"
SECOND = "1760781630.000100"  # 30 seconds later


def post(ts: str, text: str, user: str = "U0SAM", **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "text": text, "user": user, **extra}


class History(FakeReader):
    """FakeReader plus the channel's top-level posts, newest first, as Slack answers."""

    def __init__(self, posts: list[dict[str, Any]], *, fail: bool = False) -> None:
        super().__init__({p["ts"]: [p] for p in posts})
        self.posts = posts
        self.fail = fail

    async def history(
        self, channel: str, *, oldest: str, latest: str, limit: int
    ) -> Mapping[str, Any]:
        self.calls.append(("history", latest))
        if self.fail:
            raise ConnectionError("slack")
        found = [p for p in self.posts if float(oldest) < float(p["ts"]) < float(latest)]
        return {"messages": sorted(found, key=lambda p: p["ts"], reverse=True)[:limit]}


async def follows(reader: History, store: MemoryStore | None = None) -> Any:
    work = await SlackWorkSource(reader, store or MemoryStore()).thread(CH, SECOND)
    return work.follows


async def test_the_packet_names_the_authors_post_just_before_with_its_topic() -> None:
    store = MemoryStore()
    topic = (await store.create_topic(CH, "Line screens")).topic
    await store.link_message(MessageRef(channel_id=CH, ts=FIRST), topic.id)
    reader = History([post(FIRST, "Show it on the west screens."), post(SECOND, "And the east.")])
    found = await follows(reader, store)
    assert found is not None
    assert (found.ts, found.seconds_before, found.topic_id) == (FIRST, 30, topic.id)


async def test_only_the_same_authors_nearest_top_level_post_within_the_window() -> None:
    older = f"{float(SECOND) - 90:.6f}"
    reader = History(
        [
            post(older, "Earlier."),
            post(FIRST, "Nearest.", thread_ts=FIRST),  # a thread's parent is top-level
            post("1760781610.000100", "A reply", thread_ts="1760781000.000100"),
            post("1760781620.000100", "Someone else", user="U0ALEX"),
            post(SECOND, "And the east."),
        ]
    )
    found = await follows(reader)
    assert found is not None and found.ts == FIRST


async def test_no_hint_past_the_window_for_a_bot_or_when_slack_fails() -> None:
    late = f"{float(SECOND) - FOLLOWS_WITHIN - 1:.6f}"
    assert await follows(History([post(late, "Long ago."), post(SECOND, "New.")])) is None
    assert await follows(History([post(FIRST, "x"), post(SECOND, "y")], fail=True)) is None
    bot = History([post(FIRST, "x", bot_id="B1"), post(SECOND, "y")])
    assert await follows(bot) is None


async def test_no_hint_for_a_post_a_person_said_skip_for() -> None:
    store = MemoryStore()
    await store.mark_chatter(MessageRef(channel_id=CH, ts=FIRST), by="person")
    reader = History([post(FIRST, "skip: not this"), post(SECOND, "And the east.")])
    assert await follows(reader, store) is None


# The debouncer


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


POLICY = parse_policy(
    f"""
debounce_seconds = 60

[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "Web"
"""
)


def top(ts: str) -> Accepted:
    return Accepted(kind="message", ref=MessageRef(channel_id=CH, ts=ts))


async def test_a_quick_second_post_waits_for_the_first_ones_run() -> None:
    clock = Clock()
    debouncer = Debouncer(MemoryStore(), POLICY, clock=clock)
    await debouncer.note(top(FIRST), "U0SAM")
    clock.now += timedelta(seconds=30)
    await debouncer.note(top(SECOND), "U0SAM")
    due = debouncer.pending()
    first, second = due[Thread(CH, FIRST)], due[Thread(CH, SECOND)]
    assert second == first + timedelta(seconds=60)  # a window after the first is due


async def test_another_author_or_a_slow_second_post_waits_as_usual() -> None:
    clock = Clock()
    debouncer = Debouncer(MemoryStore(), POLICY, clock=clock)
    await debouncer.note(top(FIRST), "U0SAM")
    clock.now += timedelta(seconds=30)
    await debouncer.note(top(SECOND), "U0ALEX")
    assert debouncer.pending()[Thread(CH, SECOND)] == clock.now + timedelta(seconds=60)
    late = f"{float(FIRST) + FOLLOWS_WITHIN + 5:.6f}"
    await debouncer.note(top(late), "U0SAM")
    assert debouncer.pending()[Thread(CH, late)] == clock.now + timedelta(seconds=60)
