"""One message, several topics (#335): a pasted email with separate actions.

The architect's store conditions are pinned here: the cap and the agent-only rule
in the store, a person's decision clearing the split, topics listing the message,
the packet showing it, the debouncer leaving a split parent's replies alone,
credits and follow-ups reaching the second topic, and rebuild restoring it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jotflow.models import MessageRef
from jotflow.store import ConflictError, MemoryStore, SqliteStore, Store
from jotflow.store.base import ALSO_NOT_LINKED, ALSO_PERSON, ALSO_TOO_MANY

CH = "C0EXAMPLE1"
PASTE = "1792360000.000100"
REPLY = "1792360100.000100"


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    made: Store = (
        MemoryStore()
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db")
    )
    yield made
    await made.close()


def paste() -> MessageRef:
    return MessageRef(channel_id=CH, ts=PASTE)


async def split(store: Store, *titles: str) -> list[str]:
    """The paste in its first topic, then each other title as an extra one."""
    first = (await store.create_topic(CH, "Mixer")).topic.id
    await store.link_message(paste(), first)
    ids = [first]
    for title in titles:
        other = (await store.create_topic(CH, title)).topic.id
        await store.also_link(paste(), other)
        ids.append(other)
    return ids


# The store


async def test_a_message_feeds_up_to_three_topics_each_listing_it(store: Store) -> None:
    mixer, sensor, labels = await split(store, "Sensor timing", "Labels")
    link = await store.get_message(CH, PASTE)
    assert link is not None and link.topic_id == mixer
    assert link.also_topics == (sensor, labels)
    for topic_id in (mixer, sensor, labels):
        found = await store.get_topic(topic_id)
        assert found is not None and [m.ts for m in found.topic.messages] == [PASTE]
    fourth = (await store.create_topic(CH, "Fourth")).topic.id
    with pytest.raises(ConflictError, match=ALSO_TOO_MANY):
        await store.also_link(paste(), fourth)
    assert (await store.also_link(paste(), sensor)).also_topics == (sensor, labels)  # once


async def test_only_over_the_agents_own_placement(store: Store) -> None:
    other = (await store.create_topic(CH, "Sensor timing")).topic.id
    with pytest.raises(ConflictError, match=ALSO_NOT_LINKED):
        await store.also_link(paste(), other)
    first = (await store.create_topic(CH, "Mixer")).topic.id
    await store.link_message(paste(), first, by="person")
    with pytest.raises(ConflictError, match=ALSO_PERSON):
        await store.also_link(paste(), other)
    elsewhere = (await store.create_topic("C0EXAMPLE2", "Elsewhere")).topic.id
    await store.link_message(MessageRef(channel_id=CH, ts=REPLY), first)
    with pytest.raises(ConflictError, match="its own channel"):
        await store.also_link(MessageRef(channel_id=CH, ts=REPLY), elsewhere)


@pytest.mark.parametrize("decision", ["person moves it", "person chatter", "agent chatter"])
async def test_a_later_decision_clears_the_split(store: Store, decision: str) -> None:
    mixer, sensor = await split(store, "Sensor timing")
    if decision == "person moves it":
        await store.link_message(paste(), mixer, by="person")
    elif decision == "person chatter":
        await store.mark_chatter(paste(), by="person")
    else:
        await store.mark_chatter(paste())
    link = await store.get_message(CH, PASTE)
    assert link is not None and link.also_topics == ()
    found = await store.get_topic(sensor)
    assert found is not None and found.topic.messages == []


async def test_moving_the_first_topic_to_an_extra_one_keeps_no_duplicate(store: Store) -> None:
    _, sensor = await split(store, "Sensor timing")
    link = await store.link_message(paste(), sensor)  # the agent's own change of mind
    assert link.topic_id == sensor and link.also_topics == ()


# The tool


async def test_link_message_also_topic_starts_or_joins_a_topic() -> None:
    from jotflow.whiteboard.tools import ToolError
    from jotflow.whiteboard.write_tools import LinkMessageArgs
    from tests.test_write_tools import DRAFT, ROOT, Clock, Ids, LiveStore, Poster, WriteTools

    clock = Clock()
    live = LiveStore(clock)
    await live.record_packet("run-1", [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)])
    from tests.test_write_tools import POLICY

    tools = WriteTools(live, POLICY, Poster(), clock=clock, new_id=Ids())

    def args(**kw: Any) -> LinkMessageArgs:
        return LinkMessageArgs.model_validate({"ts": ROOT, "confidence": 0.9, **kw})

    with pytest.raises(ToolError, match="first topic"):
        await tools.link_message(DRAFT, args(decision="also_topic", title="Sensor timing"))
    first: Any = await tools.link_message(DRAFT, args(decision="new_topic", title="Mixer"))
    second: Any = await tools.link_message(DRAFT, args(decision="also_topic", title="Sensor"))
    assert second.topic_id == first.topic_id and len(second.also_topics) == 1
    with pytest.raises(ValueError, match="not both"):
        args(decision="also_topic", title="X", topic_id="t1")


# What reads topics


async def test_the_packet_shows_a_messages_other_topics() -> None:
    from jotflow.orchestrator.packet import SlackWorkSource

    store = MemoryStore()
    mixer, sensor = await split(store, "Sensor timing")

    class Reader:
        async def replies(self, channel: str, ts: str, *, cursor: Any, limit: int) -> Any:
            return {"messages": [{"ts": PASTE, "text": "Email", "user": "U0SAM"}]}

        async def user(self, user_id: str) -> Any:
            return {"user": {"profile": {"display_name": "Sam"}}}

    thread = await SlackWorkSource(Reader(), store).thread(CH, PASTE)
    [message] = thread.messages
    assert (message.topic_id, message.also_topics) == (mixer, [sensor])


async def test_a_reply_under_a_split_message_is_left_for_the_agent() -> None:
    from jotflow.orchestrator.debounce import Debouncer
    from jotflow.orchestrator.ingest import Accepted
    from tests.test_follows import POLICY as DEBOUNCE_POLICY

    store = MemoryStore()
    await split(store, "Sensor timing")
    reply = MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE)
    policy = DEBOUNCE_POLICY.model_copy(
        update={"channels": {CH: next(iter(DEBOUNCE_POLICY.channels.values()))}}
    )
    await Debouncer(store, policy).note(Accepted(kind="message", ref=reply))
    assert await store.get_message(CH, REPLY) is None  # not put on the first topic


async def test_the_second_topic_credits_the_paster() -> None:
    # #307 credits walk topic membership, so the second topic's issue names the paster.
    from datetime import UTC, datetime

    from jotflow.execution.attribution import execution_footer
    from jotflow.models import IssueProposal, ProposalRecord, ProposalState
    from tests.test_execution_attribution import Crowd, policy

    store = MemoryStore()
    _, sensor = await split(store, "Sensor timing")
    reply = MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE)
    await store.link_message(reply, sensor)
    crowd = Crowd()
    crowd._authors = {PASTE: {"U0SAM"}, REPLY: {"U0ALEX"}}  # type: ignore[assignment]
    record = ProposalRecord(
        id="p1",
        channel_id=CH,
        thread_ts=PASTE,
        run_id="r1",
        state=ProposalState.APPROVED,
        proposal=IssueProposal(
            topic_id=sensor,
            repo="acme-co/web",
            title="Sensor timing",
            body="b",
            confidence=0.9,
            sources=[reply],
        ),
        participants=["U0SAM", "U0ALEX"],
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
    )
    tail = await execution_footer(record, policy(), crowd, store)
    assert tail.splitlines()[0] == "Raised in Slack by Sam, with contributions from Alex here."


async def test_an_extra_topic_keeps_the_first_as_the_messages_topic(store: Store) -> None:
    # An extra topic never moves the message off its first.
    first = (await store.create_topic(CH, "web#1")).topic.id
    await store.link_message(paste(), first)
    second = (await store.create_topic(CH, "web#2")).topic.id
    linked = await store.get_message(CH, PASTE)
    assert linked is not None
    await store.also_link(linked.ref, second)
    again = await store.get_message(CH, PASTE)
    assert again is not None and (again.topic_id, again.also_topics) == (first, (second,))
