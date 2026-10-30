"""Replies in a weekly summary's thread (#318): re: N routes, nothing drafts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import ProposalState
from jotflow.orchestrator import summary_replies as m
from jotflow.orchestrator.keyword_handler import Person
from jotflow.orchestrator.summary import next_slot
from jotflow.orchestrator.summary_replies import SummaryReplies, note_text, plain_name
from jotflow.reactions import CONFIRM
from jotflow.store import MemoryStore, SqliteStore, Store
from tests.test_weekly_summary import CH, LONDON, SLOT, idea, policy

POST = "1793000000.000001"
WORKSPACE = "https://acme-co.slack.com/"


class Slack:
    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self.names = {"U0ALEX": "Alex <@U0EVE> *bold*", "U0SAM": "Sam"}
        self.workspace: str | None = WORKSPACE
        self.told: list[tuple[str, str, str, str]] = []
        self.reacted: list[tuple[str, str, str]] = []
        self.notes: list[tuple[str, str, str]] = []

    async def person(self, user_id: str) -> Person:
        return self.people.get(user_id, Person(user_id))

    async def name(self, user_id: str) -> str | None:
        return self.names.get(user_id)

    async def workspace_url(self) -> str | None:
        return self.workspace

    async def ephemeral(self, channel_id: str, user_id: str, thread_ts: str, text: str) -> None:
        self.told.append((channel_id, user_id, thread_ts, text))

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self.reacted.append((channel_id, ts, name))

    async def note(self, channel_id: str, thread_ts: str, text: str) -> None:
        self.notes.append((channel_id, thread_ts, text))


class Clock:
    def __init__(self) -> None:
        self.now = SLOT + timedelta(hours=1)

    def __call__(self) -> Any:
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


class Rig:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.slack = Slack()
        self.clock = Clock()
        self.seconds = 0.0
        self.replies = SummaryReplies(
            store, policy(), self.slack, clock=self.clock, monotonic=lambda: self.seconds
        )

    async def posted(self, *topics: str) -> Any:
        assert await self.store.start_summary(CH, SLOT)
        await self.store.set_summary_items(CH, SLOT, list(topics))
        await self.store.finish_summary(CH, SLOT, "posted", POST)
        post = await self.store.get_summary(CH, SLOT)
        assert post is not None
        return post

    async def reply(self, text: str, *, user: str = "U0ALEX", ts: str = "1793000100.000100") -> str:
        event = {"type": "message", "channel": CH, "ts": ts, "thread_ts": POST, "user": user}
        event["text"] = text
        post = await self.replies.summary_thread(event)
        assert post is not None
        return await self.replies.on_reply(event, post)


async def test_re_n_links_the_reply_and_notes_it_in_the_ideas_thread(store: Store) -> None:
    rig = Rig(store)
    first, _ = await idea(store, 1)
    second, _ = await idea(store, 2)
    await rig.posted(first, second)
    assert await rig.reply("re: 2 the totals row matters most") == "routed"
    link = await store.get_message(CH, "1793000100.000100")
    assert link is not None and link.topic_id == second and link.linked_by == "person"
    assert rig.slack.reacted == [(CH, "1793000100.000100", CONFIRM)]
    ((channel, thread, text),) = rig.slack.notes
    assert (channel, thread) == (CH, "1790000002.000100")  # idea 2's own thread
    assert text.startswith("Alex @U0EVE bold replied about this in the weekly summary.")
    assert "<@" not in text and "totals" not in text  # no ping, no reply text
    assert "archives/C0EXAMPLE1/p1793000100000100?thread_ts=1793000000.000001" in text
    assert rig.slack.told == []


async def test_one_note_per_person_per_item(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    await rig.reply("re: 1 one", ts="1793000100.000100")
    assert await rig.reply("re: 1 two", ts="1793000101.000100") == "linked"
    assert len(rig.slack.notes) == 1 and len(rig.slack.reacted) == 2
    await rig.reply("re: 1 three", user="U0SAM", ts="1793000102.000100")
    assert len(rig.slack.notes) == 2


async def test_a_note_without_a_workspace_url_has_no_link(store: Store) -> None:
    rig = Rig(store)
    rig.slack.workspace = None
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    await rig.reply("re: 1")
    assert rig.slack.notes[0][2] == "Alex @U0EVE bold replied about this in the weekly summary."


@pytest.mark.parametrize(
    ("text", "told"),
    [
        ("re: 7", "That summary has no item 7."),
        ("re: 0", "That summary has no item 0."),
        ("re: web#42", m.NEEDS_NUMBER),
        ("re: two", m.NEEDS_NUMBER),
    ],
)
async def test_a_bad_number_gets_a_private_reply(store: Store, text: str, told: str) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    assert await rig.reply(text) == "refused"
    assert rig.slack.told == [(CH, "U0ALEX", POST, told)]
    assert rig.slack.notes == [] and await store.get_message(CH, "1793000100.000100") is None


async def test_an_older_summary_has_passed(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    rig.clock.now = next_slot(SLOT, LONDON) + timedelta(minutes=1)
    assert await rig.reply("re: 1") == "refused"
    assert rig.slack.told[-1][3] == m.PASSED


@pytest.mark.parametrize("state", [ProposalState.REJECTED, ProposalState.SUPERSEDED])
async def test_a_removed_idea_isnt_routed(store: Store, state: ProposalState) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1, state=state)
    await rig.posted(topic)
    assert await rig.reply("re: 1") == "refused"
    assert rig.slack.told[-1][3] == m.NOT_OPEN and rig.slack.notes == []


async def test_a_guest_routes_nothing_and_hears_nothing(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    rig.slack.people["U0ALEX"] = Person("U0ALEX", guest=True)
    assert await rig.reply("re: 1") == "ignored"
    assert rig.slack.told == [] and rig.slack.notes == [] and rig.slack.reacted == []


async def test_anything_else_is_pointed_to_the_channel_once(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    assert await rig.reply("We should also export to Excel") == "pointed"
    assert await rig.reply("And PDF", ts="1793000101.000100") == "left"
    assert [t[3] for t in rig.slack.told] == [m.NEW_IDEAS_HERE]
    assert await store.get_message(CH, "1793000100.000100") is None


async def test_a_question_there_needs_no_pointer(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    post = await rig.posted(topic)
    event = {"channel": CH, "ts": "1793000100.000100", "thread_ts": POST, "user": "U0ALEX"}
    event["text"] = "ask: what is item 1 about?"
    assert await rig.replies.on_reply(event, post, asked=True) == "left"
    assert rig.slack.told == []


async def test_private_replies_are_limited_per_person(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    for n in range(m.MAX_TOLD + 3):
        await rig.reply(f"re: {n + 50}", ts=f"1793000{n:03d}.000100")
    assert len(rig.slack.told) == m.MAX_TOLD
    rig.seconds += m.TOLD_WINDOW_SECONDS
    await rig.reply("re: 99", ts="1793009999.000100")
    assert len(rig.slack.told) == m.MAX_TOLD + 1


async def test_only_a_reply_in_a_posted_summarys_thread_counts(store: Store) -> None:
    rig = Rig(store)
    topic, _ = await idea(store, 1)
    await rig.posted(topic)
    base = {"channel": CH, "user": "U0ALEX", "text": "re: 1"}
    assert await rig.replies.summary_thread({**base, "ts": POST}) is None  # the post itself
    other = {**base, "ts": "1793000100.000100", "thread_ts": "1790000001.000100"}
    assert await rig.replies.summary_thread(other) is None
    assert await rig.replies.summary_thread({**base, "ts": "1.2", "thread_ts": "x"}) is None


async def test_a_summarys_items_drop_older_summaries(store: Store) -> None:
    later = SLOT + timedelta(days=7)
    await store.set_summary_items(CH, SLOT, ["a", "b"])
    await store.set_summary_items("C0EXAMPLE2", SLOT, ["x"])
    await store.set_summary_items(CH, later, ["c", "a"])
    assert await store.summary_items(CH, SLOT) == []
    assert await store.summary_items(CH, later) == ["c", "a"]
    assert await store.summary_items("C0EXAMPLE2", SLOT) == ["x"]


def test_the_note_and_names_are_jotflows_own_text() -> None:
    assert plain_name("<@U0EVE> &amp; `x`") == "@U0EVE amp; x"
    assert plain_name("") == "Someone" and plain_name(None) == "Someone"
    assert note_text("Alex", None) == "Alex replied about this in the weekly summary."
    assert note_text("Alex", "https://x/y").endswith(" <https://x/y|See the reply.>")
