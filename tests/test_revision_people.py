"""A revision keeps the people already on a card (#378), on both stores.

A kept named_in_text reason keeps the name JotFlow matched, a person's add on the
card survives a revision that leaves it out, and a person's own Add me request is
never offered as a topic's draft or revised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import (
    AssigneeReason,
    IssueChanges,
    IssueProposal,
    MessageRef,
    PendingDraft,
    ProposalRecord,
    RunKind,
    UpdateProposal,
)
from jotflow.name_match import PACKET_PEOPLE
from jotflow.orchestrator.card_edits import with_assignees
from jotflow.orchestrator.cards import CardContext, render_card
from jotflow.store import MemoryStore, SqliteStore, Store
from jotflow.store.base import SELF_ADD_RUN, ToolCallRecord
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import (
    ProposeUpdateArgs,
    RevisedIssue,
    ReviseProposalArgs,
    WriteTools,
)
from tests.test_assignee_reasons import POLICY, Authors, Mentions, Texts, asked, names_of
from tests.test_write_tools import CH, REPLY, ROOT, Clock, Ids, Poster, link


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


async def drafting(store: Store, clock: Clock, text: str = "Assign to Alice please") -> Any:
    """A running drafting run on ROOT, its packet and people, and its write tools."""
    run = await store.enqueue_run(RunKind.DRAFTING, CH, ROOT)
    claimed = await store.claim_next_run(max_running=8)
    assert claimed is not None and claimed.id == run.id
    grant = Grant(run.id, RunKind.DRAFTING, CH, thread_ts=ROOT)
    await store.record_packet(
        run.id,
        [
            MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT),
            MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT),
        ],
    )
    await store.record_tool_call(
        ToolCallRecord(
            run_id=run.id,
            tool=PACKET_PEOPLE,
            args_hash="people",
            channel_id=CH,
            thread_ts=ROOT,
            result_json=json.dumps(["U0ALICE", "U0BOB"]),
            called_at=clock(),
        )
    )
    await store.link_identity("U0BOB", "acme-bob")
    await store.finish_verification("U0ALICE", github_login="acme-alice", github_id=11)
    tools = WriteTools(
        store,
        POLICY,
        Poster(),
        clock=clock,
        new_id=Ids(),
        authors=Authors(),
        mentions=Mentions(),
        texts=Texts(text),
        names=names_of,
    )
    topic: Any = await tools.link_message(grant, link())
    return tools, grant, str(topic.topic_id)


def revision(record: ProposalRecord, assignees: list[str], reasons: list[Any]) -> Any:
    return ReviseProposalArgs(
        proposal_id=record.id,
        base_revision=record.revision,
        issue=RevisedIssue(
            title="Export the dashboard table as CSV",
            body="People want a CSV export, monthly.",
            labels=["enhancement"],
            assignees=assignees,
            assignee_reasons=reasons,
        ),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )


async def named_alice(store: Store, clock: Clock) -> tuple[WriteTools, Grant, ProposalRecord]:
    tools, grant, topic_id = await drafting(store, clock)
    made: Any = await tools.propose_issue(
        grant, asked(topic_id, "acme-alice", "named_in_text", ts=ROOT)
    )
    record = await store.get_proposal(made.proposal_id)
    assert record is not None
    return tools, grant, record


async def test_a_kept_name_in_words_keeps_its_matched_name(store: Store, clock: Clock) -> None:
    tools, grant, record = await named_alice(store, clock)
    shown = PendingDraft.of(record)
    assert [(r.login, r.why, r.matched) for r in shown.assignee_reasons] == [
        ("acme-alice", "named_in_text", None)  # Slack's name stays off the packet
    ]
    result: Any = await tools.revise_proposal(
        grant, revision(record, ["acme-alice"], list(shown.assignee_reasons))
    )
    assert result.revised
    revised = await store.get_proposal(record.id)
    assert revised is not None and isinstance(revised.proposal, IssueProposal)
    (reason,) = revised.proposal.assignee_reasons
    assert (reason.why, reason.matched) == ("named_in_text", "Alice")
    card = render_card(revised, CardContext(channel=POLICY.channel(CH)))
    assert "named in the thread ('Alice')" in str(card.blocks)


async def card_add(store: Store, record: ProposalRecord) -> ProposalRecord:
    """Bob adds himself on the card."""
    edited = with_assignees(record.proposal, ["acme-alice", "acme-bob"], added="acme-bob")
    await store.edit_proposal(record.id, edited, by="U0BOB", field="assign")
    current = await store.get_proposal(record.id)
    assert current is not None
    return current


async def test_the_packet_shows_a_card_add_and_a_revision_can_keep_it(
    store: Store, clock: Clock
) -> None:
    tools, grant, record = await named_alice(store, clock)
    current = await card_add(store, record)
    shown = PendingDraft.of(current)
    assert [(r.login, r.why) for r in shown.assignee_reasons] == [
        ("acme-alice", "named_in_text"),
        ("acme-bob", "added_on_card"),
    ]
    result: Any = await tools.revise_proposal(
        grant, revision(current, ["acme-alice", "acme-bob"], list(shown.assignee_reasons))
    )
    assert result.revised
    revised = await store.get_proposal(record.id)
    assert revised is not None and isinstance(revised.proposal, IssueProposal)
    assert [(r.login, r.why, r.matched) for r in revised.proposal.assignee_reasons] == [
        ("acme-alice", "named_in_text", "Alice"),
        ("acme-bob", "added_on_card", None),
    ]


async def test_a_card_add_survives_a_revision_that_leaves_it_out(
    store: Store, clock: Clock
) -> None:
    tools, grant, record = await named_alice(store, clock)
    current = await card_add(store, record)
    alice = [r for r in PendingDraft.of(current).assignee_reasons if r.login == "acme-alice"]
    result: Any = await tools.revise_proposal(grant, revision(current, ["acme-alice"], alice))
    assert result.revised
    revised = await store.get_proposal(record.id)
    assert revised is not None and isinstance(revised.proposal, IssueProposal)
    assert revised.proposal.assignees == ["acme-alice", "acme-bob"]
    assert [(r.login, r.why) for r in revised.proposal.assignee_reasons] == [
        ("acme-alice", "named_in_text"),
        ("acme-bob", "added_on_card"),
    ]


async def test_the_agent_still_never_claims_a_card_add_in_a_revision(
    store: Store, clock: Clock
) -> None:
    tools, grant, record = await named_alice(store, clock)
    claimed = AssigneeReason(login="acme-bob", why="added_on_card")
    alice = list(PendingDraft.of(record).assignee_reasons)
    with pytest.raises(ToolError, match="added_on_card is a person's reason"):
        await tools.revise_proposal(
            grant, revision(record, ["acme-alice", "acme-bob"], [*alice, claimed])
        )


async def test_a_revision_never_unassigns_a_card_add_either(store: Store, clock: Clock) -> None:
    tools, grant, topic_id = await drafting(store, clock)
    await store.set_topic_issue(topic_id, "acme-co/web", 7)
    made: Any = await tools.propose_update(
        grant,
        ProposeUpdateArgs.model_validate(
            {
                "topic_id": topic_id,
                "repo": "acme-co/web",
                "issue_number": 7,
                "changes": {"comment": "Also export the totals."},
                "sources": [{"channel_id": CH, "ts": REPLY}],
                "confidence": 0.9,
            }
        ),
    )
    record = await store.get_proposal(made.proposal_id)
    assert record is not None
    edited = with_assignees(record.proposal, ["acme-bob"], added="acme-bob")
    await store.edit_proposal(record.id, edited, by="U0BOB", field="assign")
    current = await store.get_proposal(record.id)
    assert current is not None
    revised = ReviseProposalArgs(
        proposal_id=current.id,
        base_revision=current.revision,
        changes=IssueChanges(
            comment="Also export the totals, monthly.", remove_assignees=["Acme-Bob"]
        ),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )
    result: Any = await tools.revise_proposal(grant, revised)
    assert result.revised
    after = await store.get_proposal(record.id)
    assert after is not None and isinstance(after.proposal, UpdateProposal)
    assert after.proposal.changes.add_assignees == ["acme-bob"]
    assert after.proposal.changes.remove_assignees == []
    assert after.proposal.changes.comment == "Also export the totals, monthly."


async def add_me_request(store: Store, clock: Clock, topic_id: str) -> ProposalRecord:
    """Bob's own Add me request on the topic's filed issue."""
    await store.set_topic_issue(topic_id, "acme-co/web", 7)
    draft = UpdateProposal(
        topic_id=topic_id,
        repo="acme-co/web",
        issue_number=7,
        changes=IssueChanges(
            add_assignees=["acme-bob"],
            assignee_reasons=[AssigneeReason(login="acme-bob", why="added_on_card")],
        ),
        confidence=1.0,
    )
    return await store.add_proposal(
        ProposalRecord(
            id="aaaaaaaaaaaaaaaa",
            channel_id=CH,
            thread_ts=ROOT,
            run_id=SELF_ADD_RUN,
            proposal=draft,
            participants=["U0BOB"],
            created_at=clock(),
        )
    )


async def test_an_add_me_request_is_never_revised(store: Store, clock: Clock) -> None:
    tools, grant, topic_id = await drafting(store, clock)
    record = await add_me_request(store, clock, topic_id)
    revised = ReviseProposalArgs(
        proposal_id=record.id,
        base_revision=record.revision,
        changes=IssueChanges(
            comment="Alice will own this.",
            add_assignees=["acme-bob", "acme-alice"],
            assignee_reasons=[
                AssigneeReason(login="acme-bob", why="added_on_card"),
                AssigneeReason(login="acme-alice", why="named_in_text", source_ts=ROOT),
            ],
        ),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )
    with pytest.raises(ToolError, match="a person's Add me request: propose a separate update"):
        await tools.revise_proposal(grant, revised)
    after = await store.get_proposal(record.id)
    assert after is not None and after.revision == record.revision
    assert after.participants == ["U0BOB"]
    assert after.proposal == record.proposal


async def test_an_add_me_request_is_not_a_topics_pending_draft(store: Store, clock: Clock) -> None:
    from jotflow.whiteboard.read_tools import Work
    from tests.test_read_tools import CH as IDEAS
    from tests.test_read_tools import POST, call, running, tools

    topic_id = (await store.create_topic(IDEAS, "CSV export")).topic.id
    await store.link_message(MessageRef(channel_id=IDEAS, ts=POST, thread_ts=POST), topic_id)
    await store.set_topic_issue(topic_id, "acme-co/web", 7)
    draft = UpdateProposal(
        topic_id=topic_id,
        repo="acme-co/web",
        issue_number=7,
        changes=IssueChanges(
            add_assignees=["acme-bob"],
            assignee_reasons=[AssigneeReason(login="acme-bob", why="added_on_card")],
        ),
        confidence=1.0,
    )
    await store.add_proposal(
        ProposalRecord(
            id="aaaaaaaaaaaaaaaa",
            channel_id=IDEAS,
            thread_ts=POST,
            run_id=SELF_ADD_RUN,
            proposal=draft,
            participants=["U0BOB"],
            created_at=clock(),
        )
    )
    work: Work = await call(tools(store, clock)["get_work"], await running(store))  # type: ignore[arg-type]
    assert work.conversation is not None
    (topic,) = [t for t in work.conversation.topics.topics if t.id == topic_id]
    assert topic.pending is None
