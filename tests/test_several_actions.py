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


# Each topic keeps its own draft, and a person's decision drops the orphans (#335)


def draft(proposal_id: str, topic_id: str, *, thread: str = PASTE, **extra: Any) -> Any:
    from datetime import UTC, datetime

    from jotflow.models import IssueProposal, ProposalRecord

    return ProposalRecord(
        id=proposal_id,
        channel_id=CH,
        thread_ts=thread,
        run_id="r1",
        proposal=IssueProposal(
            topic_id=topic_id,
            repo="acme-co/web",
            title=f"Draft {proposal_id}",
            body="b",
            confidence=0.9,
            sources=[paste()],
        ),
        participants=["U0SAM"],
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
        **extra,
    )


async def states(store: Store) -> dict[str, str]:
    return {p.id: p.state.value for p in await store.thread_proposals(CH, PASTE)}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_the_pasted_email_keeps_a_pending_draft_for_each_topic(
    kind: str, tmp_path: Path
) -> None:
    # The second topic's proposal must not supersede the first's in the same thread.
    from jotflow.replay import replay
    from jotflow.testing.threads import builtin_thread

    made: Store = (
        MemoryStore() if kind == "memory" else await SqliteStore.open(tmp_path / "jotflow.db")
    )
    try:
        report = await replay(builtin_thread("pasted-email"), store=made)
        assert report.ok
        proposals = await made.thread_proposals(CH, PASTE)
        assert sorted(p.proposal.repo for p in proposals) == ["acme-co/api", "acme-co/web"]
        assert {p.state.value for p in proposals} == {"pending"}
    finally:
        await made.close()


async def test_a_new_draft_still_replaces_its_own_topics_older_one(store: Store) -> None:
    mixer, sensor = await split(store, "Sensor timing")
    for record in (draft("p1", mixer), draft("p2", sensor), draft("p3", mixer)):
        await store.add_proposal(record)
        await store.supersede_pending(CH, PASTE, keep_id=record.id)
    assert await states(store) == {"p1": "superseded", "p2": "pending", "p3": "pending"}


async def test_an_unsplit_thread_still_keeps_one_pending_draft(store: Store) -> None:
    first = (await store.create_topic(CH, "Mixer")).topic.id
    other = (await store.create_topic(CH, "Sensor timing")).topic.id
    await store.link_message(paste(), first)
    for record in (draft("p1", first), draft("p2", other)):
        await store.add_proposal(record)
        await store.supersede_pending(CH, PASTE, keep_id=record.id)
    assert await states(store) == {"p1": "superseded", "p2": "pending"}


@pytest.mark.parametrize("decision", ["re: the first", "re: elsewhere", "skip:"])
async def test_a_persons_decision_drops_the_extra_topics_pending_drafts(
    store: Store, decision: str
) -> None:
    from jotflow.models import ProposalState

    mixer, sensor, labels = await split(store, "Sensor timing", "Labels")
    for record in (draft("p1", mixer), draft("p2", sensor), draft("p3", labels)):
        await store.add_proposal(record)
    # The labels topic has another message, so it isn't left empty.
    await store.link_message(MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE), labels)
    if decision == "re: the first":
        link = await store.link_message(paste(), mixer, by="person")
    elif decision == "re: elsewhere":
        elsewhere = (await store.create_topic(CH, "web#42")).topic.id
        link = await store.link_message(paste(), elsewhere, by="person")
    else:
        link = await store.mark_chatter(paste(), by="person")
    # The first topic is dropped too when the message leaves it, and it's empty (#374).
    leaves_first = decision != "re: the first"
    assert link.dropped == (("p2", "p1") if leaves_first else ("p2",))
    for proposal_id in link.dropped:
        dropped = await store.get_proposal(proposal_id)
        assert dropped is not None and dropped.state is ProposalState.SUPERSEDED
        assert dropped.unsplit_at is not None and dropped.draft_cleared_at is not None
        assert dropped.revision > 1  # so its card is redrawn
    first = "superseded" if leaves_first else "pending"
    assert await states(store) == {"p1": first, "p2": "superseded", "p3": "pending"}
    again = await store.get_message(CH, PASTE)
    assert again is not None and again.dropped == ()  # only on the write's own result


async def test_a_person_moving_it_to_an_extra_topic_keeps_that_draft(store: Store) -> None:
    # The draft of the topic it moved to stays; the emptied first topic's is dropped (#374).
    mixer, sensor = await split(store, "Sensor timing")
    for record in (draft("p1", mixer), draft("p2", sensor)):
        await store.add_proposal(record)
    link = await store.link_message(paste(), sensor, by="person")
    assert link.dropped == ("p1",)
    assert await states(store) == {"p1": "superseded", "p2": "pending"}


async def test_the_first_topic_keeps_its_draft_while_it_has_messages(store: Store) -> None:
    mixer, _ = await split(store, "Sensor timing")
    await store.add_proposal(draft("p1", mixer))
    await store.link_message(MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE), mixer)
    link = await store.mark_chatter(paste(), by="person")
    assert "p1" not in link.dropped
    assert (await states(store))["p1"] == "pending"


@pytest.mark.parametrize("who", ["person skip", "person re:", "agent re-link"])
async def test_any_decision_that_empties_a_topic_drops_its_draft(store: Store, who: str) -> None:
    # #395: split or not, whoever moves a topic's last message off it drops its draft.
    only = (await store.create_topic(CH, "Mixer")).topic.id
    await store.link_message(paste(), only)
    await store.add_proposal(draft("p1", only))
    elsewhere = (await store.create_topic(CH, "web#42")).topic.id
    if who == "person skip":
        link = await store.mark_chatter(paste(), by="person")
    elif who == "person re:":
        link = await store.link_message(paste(), elsewhere, by="person")
    else:
        link = await store.link_message(paste(), elsewhere)
    assert link.dropped == ("p1",)
    dropped = await store.get_proposal("p1")
    assert dropped is not None and dropped.unsplit_at is not None
    assert await states(store) == {"p1": "superseded"}


async def test_a_topic_with_other_messages_keeps_its_draft(store: Store) -> None:
    only = (await store.create_topic(CH, "Mixer")).topic.id
    await store.link_message(paste(), only)
    await store.link_message(MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE), only)
    await store.add_proposal(draft("p1", only))
    assert (await store.mark_chatter(paste(), by="person")).dropped == ()
    assert await states(store) == {"p1": "pending"}


async def test_a_cleared_drafts_revision_counts_the_same_in_both_stores(store: Store) -> None:
    # #395: SQLite bumped twice when a state change cleared the draft; memory once.
    from jotflow.models import ProposalState

    mixer, sensor = await split(store, "Sensor timing")
    for record in (draft("p1", mixer), draft("p2", sensor), draft("p3", mixer)):
        await store.add_proposal(record)
    await store.supersede_pending(CH, PASTE, keep_id="p3")  # p1: superseded, cleared
    await store.transition("p2", ProposalState.REJECTED)  # cleared with the rejection
    revisions = {p: (await store.get_proposal(p)).revision for p in ("p1", "p2")}  # type: ignore[union-attr]
    assert revisions == {"p1": 2, "p2": 2}
    cleared = await store.clear_draft("p1")  # already cleared: no change
    assert cleared.revision == 2


async def test_an_approved_draft_on_an_orphaned_topic_is_left_alone(store: Store) -> None:
    from jotflow.models import ProposalState

    _, sensor = await split(store, "Sensor timing")
    await store.add_proposal(draft("p2", sensor))
    await store.transition("p2", ProposalState.APPROVED, approved_by="auto")
    link = await store.mark_chatter(paste(), by="person")
    assert link.dropped == ()
    kept = await store.get_proposal("p2")
    assert kept is not None and kept.state is ProposalState.APPROVED


async def test_the_agents_chatter_mark_drops_the_orphan_too(store: Store) -> None:
    _, sensor = await split(store, "Sensor timing")
    await store.add_proposal(draft("p2", sensor))
    link = await store.mark_chatter(paste())
    assert link.dropped == ("p2",)  # chatter clears the split, whoever marks it


def test_a_dropped_drafts_card_says_why() -> None:
    from datetime import UTC, datetime

    from jotflow.models import ProposalState
    from jotflow.orchestrator.cards import UNSPLIT_LINE, _state_line

    when = datetime(2026, 10, 30, tzinfo=UTC)
    record = draft("p2", "t2", state=ProposalState.SUPERSEDED, unsplit_at=when)
    line = _state_line(record)
    assert line is not None and line["elements"][0]["text"] == UNSPLIT_LINE
    plain = _state_line(draft("p3", "t3", state=ProposalState.SUPERSEDED))
    assert plain is not None and plain["elements"][0]["text"] != UNSPLIT_LINE


# #307 credits with a split message


async def credit_line(store: Store, topic_id: str, crowd: Any) -> str:
    from jotflow.execution.attribution import execution_footer
    from jotflow.models import ProposalState
    from tests.test_execution_attribution import policy

    record = draft("p9", topic_id, state=ProposalState.APPROVED)
    return (await execution_footer(record, policy(), crowd, store)).splitlines()[0]


async def test_a_split_messages_author_is_credited_on_each_idea_in_the_thread() -> None:
    from tests.test_execution_attribution import Crowd

    store = MemoryStore()
    topics = await split(store, "Sensor timing", "Labels")
    crowd = Crowd()
    crowd._authors = {PASTE: {"U0SAM"}}  # type: ignore[assignment]
    for topic_id in topics:
        assert await credit_line(store, topic_id, crowd) == "Raised in Slack by Sam."


async def test_an_also_link_from_another_thread_credits_nobody() -> None:
    from tests.test_execution_attribution import Crowd

    store = MemoryStore()
    _, sensor = await split(store, "Sensor timing")
    other = MessageRef(channel_id=CH, ts="1792360300.000100")
    elsewhere = (await store.create_topic(CH, "Elsewhere")).topic.id
    await store.link_message(other, elsewhere)
    await store.also_link(other, sensor)
    crowd = Crowd()
    crowd._authors = {PASTE: {"U0SAM"}, other.ts: {"U0OUTSIDE"}}  # type: ignore[assignment]
    assert await credit_line(store, sensor, crowd) == "Raised in Slack by Sam."


async def test_an_add_me_request_on_an_orphaned_topic_is_kept(store: Store) -> None:
    # A person's Add me request (#305) is never superseded, by a split's undoing either.
    from jotflow.store.base import SELF_ADD_RUN

    _, sensor = await split(store, "Sensor timing")
    await store.add_proposal(draft("p2", sensor).model_copy(update={"run_id": SELF_ADD_RUN}))
    link = await store.mark_chatter(paste(), by="person")
    assert link.dropped == ()
    assert await states(store) == {"p2": "pending"}


async def test_an_older_schema_drops_an_emptied_topics_draft_too(tmp_path: Path) -> None:
    # #395 review: before migration 23 there's no message_topics and no unsplit_at, and
    # an emptied topic's draft is still dropped, with neither touched.
    from jotflow.models import ProposalState
    from jotflow.store.migrations import MIGRATIONS

    old = await SqliteStore.open(tmp_path / "old.db", migrations=MIGRATIONS[:22])
    try:
        only = (await old.create_topic(CH, "Mixer")).topic.id
        await old.link_message(paste(), only)
        await old.add_proposal(draft("p1", only))
        link = await old.mark_chatter(paste(), by="person")
        assert link.dropped == ("p1",)
        dropped = await old.get_proposal("p1")
        assert dropped is not None and dropped.state is ProposalState.SUPERSEDED
        assert dropped.unsplit_at is None and dropped.draft_cleared_at is not None
    finally:
        await old.close()


async def test_the_store_refuses_a_draft_for_an_empty_topic_when_asked(store: Store) -> None:
    # #395 review: checked in the insert's own write, so nothing can empty it in between.
    from jotflow.store.base import EMPTY_TOPIC

    empty = (await store.create_topic(CH, "Nothing yet")).topic.id
    with pytest.raises(ConflictError, match=EMPTY_TOPIC):
        await store.add_proposal(draft("p1", empty), needs_message=True)
    await store.add_proposal(draft("p2", empty))  # JotFlow's own, unasked: as before
    await store.link_message(paste(), empty)
    await store.add_proposal(draft("p3", empty), needs_message=True)


# #391: a moved message takes its file off another topic's pending draft

FILE_TEXT = "Mixer: one cycle.\nSensor: every second.\n"


async def with_file(store: Store, proposal_id: str, topic_id: str, *, on: str = PASTE) -> None:
    """A pending draft on ``topic_id`` attaching a file the run was served on ``on``."""
    import hashlib
    import json
    from datetime import UTC, datetime

    from jotflow.models import AttachedFile, IssueProposal, ProposalRecord
    from jotflow.store.base import SERVED_FILE, ToolCallRecord

    sha = hashlib.sha256(FILE_TEXT.encode()).hexdigest()
    run_id = f"run-{proposal_id}"
    await store.record_tool_call(
        ToolCallRecord(
            run_id=run_id,
            tool=SERVED_FILE,
            args_hash="F0DOC1",
            channel_id=CH,
            thread_ts=PASTE,
            result_json=json.dumps({"sha256": sha, "ts": on}),
            called_at=datetime(2026, 10, 30, tzinfo=UTC),
        )
    )
    attached = AttachedFile(
        file_id="F0DOC1", name="notes.txt", size=len(FILE_TEXT), sha256=sha, text=FILE_TEXT
    )
    await store.add_proposal(
        ProposalRecord(
            id=proposal_id,
            channel_id=CH,
            thread_ts=PASTE,
            run_id=run_id,
            proposal=IssueProposal(
                topic_id=topic_id,
                repo="acme-co/web",
                title="Mixer",
                body="b",
                confidence=0.9,
                sources=[paste()],
                attached=[attached],
            ),
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
        )
    )


async def attached_ids(store: Store, proposal_id: str) -> list[str]:
    from jotflow.models import IssueProposal

    record = await store.get_proposal(proposal_id)
    assert record is not None and isinstance(record.proposal, IssueProposal)
    return [a.file_id for a in record.proposal.attached]


@pytest.mark.parametrize("decision", ["re: the extra", "re: elsewhere", "skip:"])
async def test_a_moved_messages_file_leaves_the_other_topics_draft(
    store: Store, decision: str
) -> None:
    from jotflow.store import content_hash
    from jotflow.store.base import DETACHED_RUN

    mixer, sensor = await split(store, "Sensor timing")
    # The mixer topic keeps a reply, so its draft stays, only without the paste's file.
    await store.link_message(MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE), mixer)
    await with_file(store, "p1", mixer)
    before = await store.get_proposal("p1")
    assert before is not None
    if decision == "re: the extra":
        await store.link_message(paste(), sensor, by="person")
    elif decision == "re: elsewhere":
        web42 = (await store.create_topic(CH, "web#42")).topic.id
        await store.link_message(paste(), web42, by="person")
    else:
        await store.mark_chatter(paste(), by="person")
    after = await store.get_proposal("p1")
    assert after is not None and after.state.value == "pending"
    assert await attached_ids(store, "p1") == []
    assert after.revision == before.revision + 1  # the card is redrawn
    assert content_hash(after.proposal) != content_hash(before.proposal)  # an old approve is stale
    [revision] = await store.proposal_revisions("p1")
    assert (revision.run_id, revision.changed) == (DETACHED_RUN, ("attached",))


async def test_a_file_from_another_message_stays(store: Store) -> None:
    mixer, sensor = await split(store, "Sensor timing")
    reply = MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE)
    await store.link_message(reply, mixer)
    await with_file(store, "p1", mixer, on=REPLY)  # the file came on the reply
    await store.link_message(paste(), sensor, by="person")
    assert await attached_ids(store, "p1") == ["F0DOC1"]
    assert await store.proposal_revisions("p1") == []


async def test_approved_work_and_the_agents_own_move_keep_their_files(store: Store) -> None:
    from jotflow.models import ProposalState

    mixer, sensor = await split(store, "Sensor timing")
    reply = MessageRef(channel_id=CH, ts=REPLY, thread_ts=PASTE)
    await store.link_message(reply, mixer)
    await with_file(store, "p1", mixer)
    await store.transition("p1", ProposalState.APPROVED, approved_by="auto")
    await store.link_message(paste(), sensor, by="person")
    assert await attached_ids(store, "p1") == ["F0DOC1"]  # approved: as approved
    other = (await store.create_topic(CH, "Other")).topic.id
    await store.link_message(MessageRef(channel_id=CH, ts="1792360200.000100"), other)
    await with_file(store, "p2", other, on="1792360200.000100")
    await store.link_message(MessageRef(channel_id=CH, ts="1792360300.000100"), other)
    await store.link_message(MessageRef(channel_id=CH, ts="1792360200.000100"), mixer)
    assert await attached_ids(store, "p2") == ["F0DOC1"]  # the agent's own re-link


def test_a_detached_card_says_why() -> None:
    from datetime import UTC, datetime

    from jotflow.orchestrator.cards import DETACHED_LINE, CardContext, _revision_block
    from jotflow.store.base import DETACHED_RUN, ProposalRevision
    from tests.test_execution_attribution import policy

    revision = ProposalRevision(
        "p1", 2, DETACHED_RUN, ("attached",), (), datetime(2026, 10, 30, tzinfo=UTC)
    )
    context = CardContext(channel=policy().channel(CH), revisions=(revision,))
    block = _revision_block(context)
    assert block is not None and block["elements"][0]["text"] == DETACHED_LINE
