from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jotflow.models import TOPIC_INDEX_NOTE, MessageRef, TopicIndex
from jotflow.orchestrator.topics import MAX_THREADS, build_index, list_topics
from jotflow.policy import Policy, PolicyError, UnknownChannelError, parse_policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore, SqliteStore, Store

CH = "C0IDEAS01"
START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path, clock: Clock):  # type: ignore[no-untyped-def]
    made: Store = (
        MemoryStore(clock=clock)
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)
    )
    yield made
    await made.close()


def policy(**overrides: object) -> Policy:
    data: dict[str, object] = {
        "channels": {
            CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]}
        }
    }
    data.update(overrides)
    return Policy.model_validate(data)


def ref(ts: str, thread: str | None = None) -> MessageRef:
    return MessageRef(channel_id=CH, ts=ts, thread_ts=thread)


async def test_only_topics_active_in_the_window(store: Store, clock: Clock) -> None:
    old = (await store.create_topic(CH, "Old idea")).topic
    clock.now = START + timedelta(days=10)
    recent = (await store.create_topic(CH, "Recent idea")).topic
    clock.now = START + timedelta(days=15)

    index = await build_index(store, CH, window_days=7, now=clock.now)
    assert [t.id for t in index.topics] == [recent.id]
    assert index.window_days == 7 and index.generated_at == clock.now

    wider = await build_index(store, CH, window_days=30, now=clock.now)
    assert [t.id for t in wider.topics] == [recent.id, old.id]


async def test_the_window_edge_is_inclusive(store: Store, clock: Clock) -> None:
    topic = (await store.create_topic(CH, "Edge")).topic
    on_edge = START + timedelta(days=7)
    assert [t.id for t in (await build_index(store, CH, window_days=7, now=on_edge)).topics] == [
        topic.id
    ]
    past = on_edge + timedelta(microseconds=1)
    assert (await build_index(store, CH, window_days=7, now=past)).topics == []


async def test_linking_a_message_brings_a_topic_back(store: Store, clock: Clock) -> None:
    topic = (await store.create_topic(CH, "Revisited")).topic
    clock.now = START + timedelta(days=30)
    assert (await build_index(store, CH, window_days=14, now=clock.now)).topics == []
    await store.link_message(ref("1761813600.000100"), topic.id)
    index = await build_index(store, CH, window_days=14, now=clock.now)
    assert [t.id for t in index.topics] == [topic.id]
    assert index.topics[0].last_active == clock.now


async def test_summary_fields(store: Store, clock: Clock) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    await store.set_topic_issue(topic.id, "acme-co/web", 42)
    for i in range(8):
        clock.now = START + timedelta(minutes=i)
        thread = f"17593{i:05d}.000100"
        await store.link_message(ref(thread), topic.id)
        await store.link_message(ref(f"17593{i:05d}.000200", thread=thread), topic.id)
    (summary,) = (await build_index(store, CH, window_days=14, now=clock.now)).topics
    assert (summary.repo, summary.issue_number, summary.message_count) == ("acme-co/web", 42, 16)
    assert len(summary.threads) == MAX_THREADS
    assert summary.threads[0] == "1759300007.000100"
    assert summary.threads == sorted(summary.threads, reverse=True)


async def test_other_channels_are_not_shown(store: Store) -> None:
    await store.create_topic("C0IDEAS02", "Elsewhere")
    assert (await build_index(store, CH, window_days=14, now=START)).topics == []


async def test_the_index_is_capped(store: Store, clock: Clock) -> None:
    for i in range(5):
        clock.now = START + timedelta(minutes=i)
        await store.create_topic(CH, f"Idea {i}")
    index = await build_index(store, CH, window_days=14, now=clock.now, limit=3)
    assert [t.title.text for t in index.topics] == ["Idea 4", "Idea 3", "Idea 2"]
    assert index.more == 2
    assert json.loads(index.to_prompt())["more"] == 2


@pytest.mark.parametrize(("window", "limit"), [(0, 50), (14, 0)])
async def test_bad_window_or_limit(store: Store, window: int, limit: int) -> None:
    with pytest.raises(ValueError):
        await build_index(store, CH, window_days=window, limit=limit)


async def test_compact_prompt_form(store: Store, clock: Clock) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    await store.link_message(ref("1759309200.000100"), topic.id)
    index = await build_index(store, CH, window_days=14, now=clock.now)
    text = index.to_prompt()
    assert "\n" not in text and ": " not in text and "null" not in text
    data = json.loads(text)
    assert data["note"] == TOPIC_INDEX_NOTE
    # The compact form keeps the untrusted label on every title (#14).
    assert data["topics"][0]["title"] == {"untrusted": True, "text": "Progress bar"}
    assert "repo" not in data["topics"][0]
    assert "more" not in data
    assert TopicIndex.model_validate_json(text) == index


async def test_titles_are_cleaned_again_on_the_way_out(store: Store, clock: Clock) -> None:
    created = await store.create_topic(CH, "Fine title")
    # Simulate a title that reached the store some other way.
    if isinstance(store, MemoryStore):
        record = store._topics[created.topic.id]
        store._topics[created.topic.id] = type(record)(
            record.topic.model_copy(update={"title": "<!channel> click https://evil.example"}),
            record.created_at,
            record.last_activity_at,
        )
    else:
        assert isinstance(store, SqliteStore)
        await store._db.execute(
            "UPDATE topics SET title = ? WHERE id = ?",
            ("<!channel> click https://evil.example", created.topic.id),
        )
    (summary,) = (await build_index(store, CH, window_days=14, now=clock.now)).topics
    assert summary.title.text == "click"


async def test_list_topics_uses_the_policy_window(store: Store, clock: Clock) -> None:
    await store.create_topic(CH, "Idea")
    later = START + timedelta(days=10)
    assert len((await list_topics(store, policy(), CH, now=later)).topics) == 1
    narrow = policy(topic_window_days=3)
    index = await list_topics(store, narrow, CH, now=later)
    assert index.topics == [] and index.window_days == 3


async def test_list_topics_refuses_an_unconfigured_channel(store: Store) -> None:
    with pytest.raises(UnknownChannelError):
        await list_topics(store, policy(), "C0OTHER01")


def test_window_setting_defaults_validates_and_round_trips() -> None:
    assert policy().topic_window_days == 14
    for bad in (0, 366):
        with pytest.raises(PolicyError, match="topic_window_days"):
            parse_policy(
                f"topic_window_days = {bad}\n[channels.{CH}]\n[[channels.{CH}.repos]]\n"
                'repo = "acme-co/web"\nnickname = "web"\ndescription = "Web"\n'
            )
    custom = policy(topic_window_days=30)
    assert parse_policy(render_policy(custom)).topic_window_days == 30
