"""A pending draft revised from new messages (#304), in both stores."""

from __future__ import annotations

from pathlib import Path

import pytest

from jotflow.models import IssueChanges, IssueProposal, MessageRef, ProposalState, UpdateProposal
from jotflow.store import MemoryStore, SqliteStore, content_hash
from jotflow.store.base import (
    MAX_REVISIONS,
    StaleRevisionError,
    Store,
    TransitionError,
    revision_changes,
)
from tests.test_runs import CH, THREAD, _proposal_record

REPLY = MessageRef(channel_id=CH, ts="1792310460.000100", thread_ts=THREAD)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    made: Store = (
        MemoryStore()
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db")
    )
    yield made
    await made.close()


def revised(base: IssueProposal, **fields: object) -> IssueProposal:
    return base.model_copy(update={"sources": [REPLY], **fields})


async def test_a_revision_replaces_the_draft_and_records_who_and_what(store: Store) -> None:
    added = await store.add_proposal(_proposal_record())
    draft = added.proposal
    assert isinstance(draft, IssueProposal)
    new = revised(draft, body="Add one, with a percentage.", labels=["enhancement"])
    after = await store.revise_proposal(
        "p1", new, run_id="run-2", base_revision=added.revision, added_by=["U0BOB", "U0BOB"]
    )
    assert after.revision == added.revision + 1
    assert after.proposal.body == "Add one, with a percentage."  # type: ignore[union-attr]
    assert after.proposal.sources == [REPLY]
    assert "U0BOB" in after.participants
    # The old card's hash no longer approves: the draft changed.
    assert content_hash(after.proposal) != content_hash(draft)
    [revision] = await store.proposal_revisions("p1")
    assert (revision.revision, revision.run_id) == (after.revision, "run-2")
    assert revision.changed == ("body", "labels")
    assert revision.added_by == ("U0BOB",)


async def test_the_repo_topic_and_sources_stay_the_proposals_own(store: Store) -> None:
    topic = (await store.create_topic(CH, "Progress")).topic.id
    first = await store.add_proposal(
        _proposal_record().model_copy(
            update={
                "proposal": IssueProposal(
                    repo="acme-co/web",
                    title="Progress bar",
                    body="Add one.",
                    confidence=0.9,
                    topic_id=topic,
                    sources=[MessageRef(channel_id=CH, ts=THREAD)],
                )
            }
        )
    )
    draft = first.proposal
    assert isinstance(draft, IssueProposal)
    moved = revised(draft, repo="acme-co/api", topic_id="t9", title="Progress bars")
    after = await store.revise_proposal(
        "p1", moved, run_id="r", base_revision=first.revision, added_by=["U0BOB"]
    )
    assert (after.proposal.repo, after.proposal.topic_id) == ("acme-co/web", topic)
    assert [s.ts for s in after.proposal.sources] == [THREAD, REPLY.ts]


async def test_a_stale_base_revision_is_refused_with_the_current_record(store: Store) -> None:
    added = await store.add_proposal(_proposal_record())
    draft = added.proposal
    assert isinstance(draft, IssueProposal)
    await store.revise_proposal(
        "p1", revised(draft, body="v2"), run_id="r1", base_revision=added.revision, added_by=[]
    )
    with pytest.raises(StaleRevisionError) as stale:
        await store.revise_proposal(
            "p1", revised(draft, body="v3"), run_id="r2", base_revision=added.revision, added_by=[]
        )
    assert stale.value.record.proposal.body == "v2"  # type: ignore[union-attr]
    assert len(await store.proposal_revisions("p1")) == 1


async def test_only_a_pending_proposal_is_revised(store: Store) -> None:
    added = await store.add_proposal(_proposal_record())
    draft = added.proposal
    assert isinstance(draft, IssueProposal)
    approved = await store.transition(
        "p1",
        ProposalState.APPROVED,
        approved_by="U0CAROL",
        expected_hash=content_hash(draft),
    )
    with pytest.raises(TransitionError, match="can't be revised"):
        await store.revise_proposal(
            "p1",
            revised(draft, body="v2"),
            run_id="r",
            base_revision=approved.revision,
            added_by=[],
        )


async def test_a_revision_must_change_something_and_keep_its_kind(store: Store) -> None:
    added = await store.add_proposal(_proposal_record())
    draft = added.proposal
    assert isinstance(draft, IssueProposal)
    with pytest.raises(ValueError, match="changes nothing"):
        await store.revise_proposal(
            "p1", revised(draft), run_id="r", base_revision=added.revision, added_by=[]
        )
    update = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=4,
        changes=IssueChanges(comment="x"),
        confidence=0.9,
    )
    with pytest.raises(ValueError, match="keeps the proposal's kind"):
        await store.revise_proposal(
            "p1", update, run_id="r", base_revision=added.revision, added_by=[]
        )


async def test_revisions_are_capped(store: Store) -> None:
    record = await store.add_proposal(_proposal_record())
    draft = record.proposal
    assert isinstance(draft, IssueProposal)
    for n in range(MAX_REVISIONS):
        record = await store.revise_proposal(
            "p1",
            revised(draft, body=f"v{n}"),
            run_id="r",
            base_revision=record.revision,
            added_by=[],
        )
    with pytest.raises(ValueError, match="revised 10 times"):
        await store.revise_proposal(
            "p1",
            revised(draft, body="again"),
            run_id="r",
            base_revision=record.revision,
            added_by=[],
        )


async def test_a_follow_up_due_is_taken_once(store: Store) -> None:
    await store.add_proposal(_proposal_record())
    assert not await store.take_follow_up_due("p1")
    await store.mark_follow_up_due("p1")
    await store.mark_follow_up_due("p1")
    assert await store.take_follow_up_due("p1")
    assert not await store.take_follow_up_due("p1")


def test_an_updates_changed_fields() -> None:
    old = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=4,
        changes=IssueChanges(comment="a"),
        confidence=0.9,
    )
    new = old.model_copy(update={"changes": IssueChanges(comment="b", add_labels=["bug"])})
    assert revision_changes(old, new) == ("comment", "labels")


async def test_a_revision_past_approvals_source_cap_is_refused(store: Store) -> None:
    from jotflow.store.base import MAX_AUTHOR_LOOKUPS, TOO_MANY_SOURCES

    record = await store.add_proposal(_proposal_record())
    draft = record.proposal
    assert isinstance(draft, IssueProposal)

    def sources(n: int) -> list[MessageRef]:
        return [
            MessageRef(channel_id=CH, ts=f"1792310{500 + i:03d}.000100", thread_ts=THREAD)
            for i in range(n)
        ]

    # Up to the cap with the thread's root: still approvable, so accepted.
    at_cap = draft.model_copy(update={"body": "v2", "sources": sources(MAX_AUTHOR_LOOKUPS - 1)})
    record = await store.revise_proposal(
        "p1", at_cap, run_id="r", base_revision=record.revision, added_by=[]
    )
    over = draft.model_copy(update={"body": "v3", "sources": sources(MAX_AUTHOR_LOOKUPS)})
    with pytest.raises(ValueError, match="more than 25 messages") as refused:
        await store.revise_proposal(
            "p1", over, run_id="r", base_revision=record.revision, added_by=[]
        )
    assert str(refused.value) == TOO_MANY_SOURCES
    assert "ask in the thread instead" in TOO_MANY_SOURCES
