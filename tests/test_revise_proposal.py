"""revise_proposal: new messages revise a pending draft, safely (#304)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from jotflow.models import IssueProposal, MessageRef, ProposalRecord, ProposalState, RunKind
from jotflow.store import content_hash
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import ReviseProposalArgs, WriteTools
from tests.test_write_tools import (
    CH,
    DRAFT,
    POLICY,
    REPLY,
    ROOT,
    Clock,
    Ids,
    LiveStore,
    Poster,
    issue,
    link,
)

LATER = "1792310520.000100"


class Authors:
    """Slack's answer to who wrote some messages, as the approval gate reads it."""

    def __init__(self, by_ts: dict[str, str]) -> None:
        self.by_ts = by_ts
        self.fail = False

    async def __call__(self, refs: Sequence[MessageRef]) -> set[str]:
        if self.fail:
            raise ConnectionError("slack")
        return {self.by_ts[r.ts] for r in refs if r.ts in self.by_ts}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> LiveStore:
    return LiveStore(clock)


@pytest.fixture
def poster() -> Poster:
    return Poster()


@pytest.fixture
def authors() -> Authors:
    return Authors({ROOT: "U0ALICE", REPLY: "U0BOB", LATER: "U0CAROL"})


@pytest.fixture
async def tools(store: LiveStore, poster: Poster, clock: Clock, authors: Authors) -> WriteTools:
    packet = [MessageRef(channel_id=CH, ts=t, thread_ts=ROOT) for t in (ROOT, REPLY, LATER)]
    for run in ("run-1", "run-2", "run-3"):
        await store.record_packet(run, packet)
    return WriteTools(store, POLICY, poster, clock=clock, new_id=Ids(), authors=authors)


async def pending(tools: WriteTools, store: LiveStore) -> ProposalRecord:
    """A pending proposal on a topic this thread feeds, as a first run leaves it."""
    topic: Any = await tools.link_message(DRAFT, link())
    result: Any = await tools.propose_issue(
        DRAFT, issue(topic.topic_id, sources=[{"channel_id": CH, "ts": ROOT}])
    )
    record = await store.get_proposal(result.proposal_id)
    assert record is not None
    return record


def revise(record: ProposalRecord, **draft: Any) -> ReviseProposalArgs:
    old = record.proposal
    assert isinstance(old, IssueProposal)
    fields = {"title": old.title, "body": old.body, "labels": old.labels, **draft}
    return ReviseProposalArgs.model_validate(
        {
            "proposal_id": record.id,
            "base_revision": record.revision,
            "issue": fields,
            "sources": [{"channel_id": CH, "ts": REPLY}],
            "confidence": 0.9,
        }
    )


SECOND = Grant(run_id="run-2", kind=RunKind.DRAFTING, channel_id=CH, thread_ts=ROOT)
THIRD = Grant(run_id="run-3", kind=RunKind.DRAFTING, channel_id=CH, thread_ts=ROOT)


async def test_a_reply_revises_the_pending_draft(tools: WriteTools, store: LiveStore) -> None:
    record = await pending(tools, store)
    result: Any = await tools.revise_proposal(
        SECOND, revise(record, body="People want a CSV export, with totals.")
    )
    assert result.revised and result.changed == ["body"]
    after = await store.get_proposal(record.id)
    assert after is not None and after.revision == result.revision
    assert after.proposal.body.endswith("with totals.")  # type: ignore[union-attr]
    assert [s.ts for s in after.proposal.sources] == [ROOT, REPLY]
    [revision] = await store.proposal_revisions(record.id)
    # Who added it comes from Slack, never from the agent.
    assert revision.added_by == ("U0BOB",)
    assert "U0BOB" in after.participants
    # The card the approver saw no longer approves: its hash is the old draft's.
    assert content_hash(after.proposal) != content_hash(record.proposal)


async def test_a_stale_base_returns_the_current_draft_for_one_retry(
    tools: WriteTools, store: LiveStore
) -> None:
    record = await pending(tools, store)
    await tools.revise_proposal(SECOND, revise(record, body="Second version."))
    stale: Any = await tools.revise_proposal(THIRD, revise(record, body="Third version."))
    assert not stale.revised
    assert stale.current.body.text == "Second version."
    retry = revise(record, body="Second version, then third.").model_copy(
        update={"base_revision": stale.revision}
    )
    again: Any = await tools.revise_proposal(THIRD, retry)
    assert again.revised


async def test_a_proposal_settled_first_leaves_the_follow_up_due(
    tools: WriteTools, store: LiveStore
) -> None:
    record = await pending(tools, store)
    await store.transition(
        record.id,
        ProposalState.APPROVED,
        approved_by="U0DAVE",
        expected_hash=content_hash(record.proposal),
    )
    result: Any = await tools.revise_proposal(SECOND, revise(record, body="More."))
    assert not result.revised and result.follow_up
    assert await store.take_follow_up_due(record.id)


async def test_only_a_proposal_this_thread_feeds_is_revised(
    tools: WriteTools, store: LiveStore
) -> None:
    record = await pending(tools, store)
    elsewhere = record.model_copy(update={"id": "p-other", "thread_ts": "1792399999.000100"})
    unlinked = elsewhere.model_copy(
        update={"proposal": elsewhere.proposal.model_copy(update={"topic_id": None})}
    )
    await store.add_proposal(unlinked)
    with pytest.raises(ToolError, match="isn't on a topic this thread feeds"):
        await tools.revise_proposal(SECOND, revise(unlinked))
    with pytest.raises(ToolError, match="no proposal nope in this channel"):
        await tools.revise_proposal(
            SECOND, revise(record).model_copy(update={"proposal_id": "nope"})
        )


@pytest.mark.parametrize(
    ("draft", "problem"),
    [
        ({"labels": ["wontfix"]}, "label"),
        ({"body": "Ask @someone about it."}, "someone"),
        ({}, "changes nothing"),
    ],
)
async def test_a_revision_keeps_proposes_rules(
    tools: WriteTools, store: LiveStore, draft: dict[str, Any], problem: str
) -> None:
    record = await pending(tools, store)
    with pytest.raises(ToolError, match=problem):
        await tools.revise_proposal(SECOND, revise(record, **draft))


async def test_the_draft_must_match_the_proposals_kind(tools: WriteTools, store: LiveStore) -> None:
    record = await pending(tools, store)
    wrong = ReviseProposalArgs.model_validate(
        {
            "proposal_id": record.id,
            "base_revision": record.revision,
            "changes": {"comment": "x"},
            "sources": [{"channel_id": CH, "ts": REPLY}],
            "confidence": 0.9,
        }
    )
    with pytest.raises(ToolError, match="revise it with `issue`"):
        await tools.revise_proposal(SECOND, wrong)
    with pytest.raises(ValueError, match="exactly one of issue"):
        ReviseProposalArgs.model_validate(
            {
                "proposal_id": "p",
                "base_revision": 0,
                "sources": [{"channel_id": CH, "ts": REPLY}],
                "confidence": 0.9,
            }
        )


async def test_revisions_are_capped_per_run_apart_from_proposals(
    tools: WriteTools, store: LiveStore
) -> None:
    record = await pending(tools, store)
    for n in range(3):
        current = await store.get_proposal(record.id)
        assert current is not None
        result: Any = await tools.revise_proposal(SECOND, revise(current, body=f"Version {n}."))
        assert result.revised
    current = await store.get_proposal(record.id)
    assert current is not None
    with pytest.raises(ToolError, match="used its 3 revisions"):
        await tools.revise_proposal(SECOND, revise(current, body="Once more."))


async def test_a_slack_failure_reading_authors_refuses(
    tools: WriteTools, store: LiveStore, authors: Authors
) -> None:
    record = await pending(tools, store)
    authors.fail = True
    with pytest.raises(ToolError, match="who wrote those messages"):
        await tools.revise_proposal(SECOND, revise(record, body="More."))


async def test_a_message_outside_the_packet_is_refused(tools: WriteTools, store: LiveStore) -> None:
    record = await pending(tools, store)
    outside = revise(record, body="More.").model_copy(
        update={"sources": [MessageRef(channel_id=CH, ts="1792319999.000100")]}
    )
    with pytest.raises(ToolError, match="not in this run's packet"):
        await tools.revise_proposal(SECOND, outside)


async def test_a_lost_follow_up_drafts_the_thread_again_once_settled(
    tools: WriteTools, store: LiveStore
) -> None:
    from jotflow.orchestrator.card_sync import CardSync
    from jotflow.orchestrator.cards import CardPublisher
    from tests.test_card_sync import FakeCardPoster, FakePeople

    armed: list[tuple[str, str]] = []

    async def arm(channel_id: str, thread_ts: str) -> None:
        armed.append((channel_id, thread_ts))

    record = await pending(tools, store)
    poster = FakeCardPoster()
    sync = CardSync(store, POLICY, CardPublisher(store, poster), FakePeople(), arm_thread=arm)
    await sync.sync()
    await store.transition(record.id, ProposalState.REJECTED)
    result: Any = await tools.revise_proposal(SECOND, revise(record, body="More."))
    assert result.follow_up
    await sync.sync()  # the rejected card is redrawn, and the thread drafts again
    assert armed == [(CH, ROOT)]
    await sync.sync()
    assert armed == [(CH, ROOT)]  # taken once


async def test_an_approved_proposal_waits_for_its_write_before_the_follow_up(
    tools: WriteTools, store: LiveStore
) -> None:
    from jotflow.orchestrator.card_sync import CardSync
    from jotflow.orchestrator.cards import CardPublisher
    from tests.test_card_sync import FakeCardPoster, FakePeople

    armed: list[tuple[str, str]] = []

    async def arm(channel_id: str, thread_ts: str) -> None:
        armed.append((channel_id, thread_ts))

    record = await pending(tools, store)
    sync = CardSync(
        store, POLICY, CardPublisher(store, FakeCardPoster()), FakePeople(), arm_thread=arm
    )
    await store.transition(
        record.id,
        ProposalState.APPROVED,
        approved_by="U0DAVE",
        expected_hash=content_hash(record.proposal),
    )
    result: Any = await tools.revise_proposal(SECOND, revise(record, body="More."))
    assert result.follow_up
    await sync.sync()
    assert armed == []  # still being written: the follow-up waits for the issue
    assert await store.take_follow_up_due(record.id)  # and is still due


async def test_a_pending_update_is_revised_with_changes(
    tools: WriteTools, store: LiveStore
) -> None:
    from tests.test_write_tools import update

    topic: Any = await tools.link_message(DRAFT, link())
    await store.set_topic_issue(topic.topic_id, "acme-co/web", 42)
    proposed: Any = await tools.propose_update(DRAFT, update(topic.topic_id))
    record = await store.get_proposal(proposed.proposal_id)
    assert record is not None
    args = ReviseProposalArgs.model_validate(
        {
            "proposal_id": record.id,
            "base_revision": record.revision,
            "changes": {"comment": "Also apply the date filter, and the totals row."},
            "sources": [{"channel_id": CH, "ts": REPLY}],
            "confidence": 0.9,
        }
    )
    result: Any = await tools.revise_proposal(SECOND, args)
    assert result.revised and result.changed == ["comment", "labels"]
    from jotflow.whiteboard.write_tools import RevisedIssue

    wrong = args.model_copy(
        update={
            "changes": None,
            "issue": RevisedIssue(title="x", body="y"),
            "base_revision": result.revision,
        }
    )
    with pytest.raises(ToolError, match="revise it with `changes`"):
        await tools.revise_proposal(SECOND, wrong)
    bad = args.model_copy(
        update={
            "changes": args.changes.model_copy(update={"comment": "Ask @someone."}),  # type: ignore[union-attr]
            "base_revision": result.revision,
        }
    )
    with pytest.raises(ToolError, match="someone"):
        await tools.revise_proposal(SECOND, bad)


async def test_a_proposal_from_another_thread_is_revised_once_this_thread_feeds_its_topic(
    tools: WriteTools, store: LiveStore
) -> None:
    record = await pending(tools, store)
    other = "1792399999.000100"
    moved = record.model_copy(update={"id": "p-elsewhere", "thread_ts": other})
    await store.add_proposal(moved)
    # The topic's first message is in this run's thread, so this thread feeds it.
    result: Any = await tools.revise_proposal(SECOND, revise(moved, body="Folded in from here."))
    assert result.revised


async def test_a_discarded_failure_drafts_the_follow_up_but_a_plain_failure_waits(
    tools: WriteTools, store: LiveStore
) -> None:
    from jotflow.orchestrator.card_sync import CardSync
    from jotflow.orchestrator.cards import CardPublisher
    from tests.test_card_sync import FakeCardPoster, FakePeople

    armed: list[tuple[str, str]] = []

    async def arm(channel_id: str, thread_ts: str) -> None:
        armed.append((channel_id, thread_ts))

    record = await pending(tools, store)
    sync = CardSync(
        store, POLICY, CardPublisher(store, FakeCardPoster()), FakePeople(), arm_thread=arm
    )
    await store.mark_follow_up_due(record.id)
    await store.transition(
        record.id,
        ProposalState.APPROVED,
        approved_by="U0DAVE",
        expected_hash=content_hash(record.proposal),
    )
    await store.transition(record.id, ProposalState.EXECUTING)
    await store.transition(record.id, ProposalState.FAILED)
    await sync.sync()
    assert armed == []  # a retry may still write it
    await store.clear_draft(record.id)  # Discard
    await sync.sync()
    assert armed == [(CH, ROOT)]


async def test_a_turned_down_topic_is_marked_for_the_re_draft(
    tools: WriteTools, store: LiveStore
) -> None:
    from jotflow.whiteboard.read_tools import NoArguments, read_tools

    record = await pending(tools, store)
    specs = {s.name: s for s in read_tools(store, POLICY)}
    listed: Any = await specs["list_topics"].handler(SECOND, NoArguments())
    [before] = [t for t in listed.topics if t.id == record.proposal.topic_id]
    assert before.rejected is None and before.issue_proposed
    await store.transition(record.id, ProposalState.REJECTED)
    listed = await specs["list_topics"].handler(SECOND, NoArguments())
    [after] = [t for t in listed.topics if t.id == record.proposal.topic_id]
    assert after.rejected is True and after.issue_proposed is None
