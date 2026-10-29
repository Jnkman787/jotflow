"""Every proposed assignee has a reason JotFlow can check, shown on the card (#305)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from jotflow.models import AssigneeReason, IssueProposal, MessageRef, ProposalRecord
from jotflow.orchestrator.card_edits import with_assignees
from jotflow.orchestrator.cards import CardContext, render_card
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore
from jotflow.store.hashing import content_hash
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import WriteTools
from tests.test_write_tools import (
    CH,
    DRAFT,
    REPLY,
    ROOT,
    Clock,
    Ids,
    LiveStore,
    Poster,
    issue,
    new_topic,
    update,
)

POLICY = parse_policy(
    f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "Web"
labels = ["enhancement"]
owner_hints = ["U0OWNER"]
"""
)


class Authors:
    """Slack's word on who wrote each message: Bob wrote the reply, Alice the root."""

    async def __call__(self, refs: Sequence[MessageRef]) -> set[str]:
        return {"U0BOB" if ref.ts == REPLY else "U0ALICE" for ref in refs}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> LiveStore:
    return LiveStore(clock)


@pytest.fixture
async def tools(store: Any, clock: Any) -> WriteTools:
    packet = [
        MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT),
        MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT),
    ]
    await store.record_packet("run-1", packet)
    await store.link_identity("U0BOB", "acme-bob")  # self-declared: fine for a volunteer
    # Nominated by others, so these must be links the person proved (#305 review).
    await store.finish_verification("U0ALICE", github_login="acme-alice", github_id=11)
    await store.finish_verification("U0OWNER", github_login="acme-owner", github_id=12)
    return WriteTools(
        store, POLICY, Poster(), clock=clock, new_id=Ids(), authors=Authors(), mentions=Mentions()
    )


class Mentions:
    """Slack's word on who each message @-mentions: the root names Alice."""

    async def __call__(self, ref: MessageRef) -> set[str]:
        return {"U0ALICE"} if ref.ts == ROOT else set()


def asked(topic_id: str, login: str, why: str, ts: str | None = REPLY, **kw: Any) -> Any:
    reason: dict[str, Any] = {"login": login, "why": why}
    if ts is not None:
        reason["source_ts"] = ts
    return issue(
        topic_id,
        repo="acme-co/web",
        labels=["enhancement"],
        assignees=[login],
        assignee_reasons=[reason],
        **kw,
    )


async def test_a_volunteer_is_whoever_wrote_the_message(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(DRAFT, asked(topic_id, "acme-bob", "volunteered"))
    assert result.proposal_id
    # Alice didn't write Bob's reply, whatever the agent claims.
    other = await new_topic(tools)
    with pytest.raises(ToolError, match="didn't write message"):
        await tools.propose_issue(DRAFT, asked(other, "acme-alice", "volunteered"))


async def test_a_reasons_message_must_be_in_the_runs_packet(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="not in this run's packet"):
        await tools.propose_issue(
            DRAFT, asked(topic_id, "acme-bob", "volunteered", ts="1792399999.000100")
        )


async def test_a_named_owner_is_whoever_the_message_mentions(tools: WriteTools) -> None:
    """Slack's mentions decide, never the agent's participants list."""
    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="doesn't @-mention acme-alice"):
        await tools.propose_issue(
            DRAFT, asked(topic_id, "acme-alice", "named_owner", participants=["U0ALICE"])
        )
    result: Any = await tools.propose_issue(
        DRAFT, asked(topic_id, "acme-alice", "named_owner", ts=ROOT)
    )
    assert result.proposal_id


async def test_without_slack_a_named_owner_is_refused(store: Any, clock: Any) -> None:
    await store.record_packet("run-1", [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)])
    await store.finish_verification("U0ALICE", github_login="acme-alice", github_id=11)
    blind = WriteTools(store, POLICY, Poster(), clock=clock, new_id=Ids())
    topic_id = await new_topic(blind)
    with pytest.raises(ToolError, match="can't be checked here"):
        await blind.propose_issue(DRAFT, asked(topic_id, "acme-alice", "named_owner", ts=ROOT))


async def test_an_owner_hint_must_be_the_repos_own(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(
        DRAFT, asked(topic_id, "acme-owner", "owner_hint", ts=None)
    )
    assert result.proposal_id
    other = await new_topic(tools)
    with pytest.raises(ToolError, match="isn't one of acme-co/web's owner hints"):
        await tools.propose_issue(DRAFT, asked(other, "acme-bob", "owner_hint", ts=None))


async def test_the_agent_never_claims_a_card_add(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="a person's reason"):
        await tools.propose_issue(DRAFT, asked(topic_id, "acme-bob", "added_on_card", ts=None))


async def test_an_unlinked_login_is_never_assigned(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="hasn't linked a GitHub login"):
        await tools.propose_issue(DRAFT, asked(topic_id, "someone-else", "volunteered"))


def test_a_reason_has_its_message_exactly_when_it_needs_one() -> None:
    with pytest.raises(ValueError, match="needs the message"):
        AssigneeReason(login="a", why="volunteered")
    with pytest.raises(ValueError, match="has no source message"):
        AssigneeReason(login="a", why="owner_hint", source_ts=REPLY)
    with pytest.raises(ValueError, match="isn't an assignee"):
        IssueProposal(
            repo="acme-co/web",
            title="t",
            body="b",
            confidence=0.9,
            assignee_reasons=[AssigneeReason(login="a", why="owner_hint")],
        )


def test_old_drafts_keep_their_hash() -> None:
    plain = IssueProposal(repo="acme-co/web", title="t", body="b", confidence=0.9)
    assert "assignee_reasons" not in plain.model_dump_json(exclude_defaults=True)
    hashed = content_hash(plain)
    with_reason = plain.model_copy(
        update={
            "assignees": ["a"],
            "assignee_reasons": [AssigneeReason(login="a", why="owner_hint")],
        }
    )
    assert content_hash(with_reason) != hashed  # a reason is part of what's approved


# --- the card -------------------------------------------------------------------------------


def record(draft: IssueProposal) -> ProposalRecord:
    from datetime import UTC, datetime

    return ProposalRecord(
        id="p1",
        channel_id=CH,
        thread_ts=ROOT,
        run_id="r1",
        proposal=draft,
        created_at=datetime(2026, 10, 29, tzinfo=UTC),
    )


def test_the_card_says_why_in_its_own_words() -> None:
    draft = IssueProposal(
        repo="acme-co/web",
        title="t",
        body="b",
        confidence=0.9,
        assignees=["acme-bob", "acme-owner"],
        assignee_reasons=[
            AssigneeReason(login="acme-bob", why="volunteered", source_ts=REPLY),
            AssigneeReason(login="acme-owner", why="owner_hint"),
        ],
    )
    text = str(render_card(record(draft), CardContext(channel=POLICY.channel(CH))))
    assert "acme-bob" in text and "volunteered in the thread" in text
    assert "listed as an owner of this repo" in text


def test_a_card_edit_keeps_reasons_in_step() -> None:
    draft = IssueProposal(
        repo="acme-co/web",
        title="t",
        body="b",
        confidence=0.9,
        assignees=["acme-bob"],
        assignee_reasons=[AssigneeReason(login="acme-bob", why="volunteered", source_ts=REPLY)],
    )
    added = with_assignees(draft, ["acme-bob", "acme-kim"], added="acme-kim")
    assert isinstance(added, IssueProposal)
    assert [(r.login, r.why) for r in added.assignee_reasons] == [
        ("acme-bob", "volunteered"),
        ("acme-kim", "added_on_card"),
    ]
    removed = with_assignees(added, ["acme-kim"])
    assert isinstance(removed, IssueProposal)
    assert [r.login for r in removed.assignee_reasons] == ["acme-kim"]


async def test_a_revision_keeps_a_reason_already_on_the_draft(
    tools: WriteTools, store: MemoryStore
) -> None:
    """A person's card add has no message to prove; a revision may keep it as it is."""
    from jotflow.whiteboard.write_tools import RevisedIssue, ReviseProposalArgs

    topic_id = await new_topic(tools)
    first: Any = await tools.propose_issue(DRAFT, asked(topic_id, "acme-bob", "volunteered"))
    found = await store.get_proposal(first.proposal_id)
    assert found is not None
    edited = with_assignees(found.proposal, ["acme-bob", "acme-alice"], added="acme-alice")
    await store.edit_proposal(found.id, edited, by="U0ALICE", field="assign")
    current = await store.get_proposal(found.id)
    assert current is not None and isinstance(current.proposal, IssueProposal)
    revised = ReviseProposalArgs(
        proposal_id=found.id,
        base_revision=current.revision,
        issue=RevisedIssue(
            title="Export the dashboard table as CSV",
            body="People want a CSV export, monthly.",
            labels=["enhancement"],
            assignees=["acme-bob", "acme-alice"],
            assignee_reasons=list(current.proposal.assignee_reasons),
        ),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )
    result: Any = await tools.revise_proposal(DRAFT, revised)
    assert result.revised


@pytest.mark.parametrize(("why", "ts"), [("named_owner", ROOT), ("owner_hint", None)])
async def test_someone_nominated_needs_a_verified_link(
    tools: WriteTools, store: Any, why: str, ts: str | None
) -> None:
    """An unverified login could name a stranger's account, with nobody present claiming it."""
    await store.link_identity("U0ALICE", "acme-alice")  # back to self-declared
    await store.link_identity("U0OWNER", "acme-owner")
    login = "acme-alice" if why == "named_owner" else "acme-owner"
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(DRAFT, asked(topic_id, login, why, ts=ts))
    # The proposal goes ahead without them, and the agent is told to ask instead.
    assert result.left_off_assignees == [login]
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and record.proposal.assignees == []
    assert record.proposal.assignee_reasons == []


def owner_update(topic_id: str, **changes: Any) -> Any:
    reason = {"login": "acme-alice", "why": "named_owner", "source_ts": ROOT}
    return update(
        topic_id,
        changes={"add_assignees": ["acme-alice"], "assignee_reasons": [reason], **changes},
        sources=[{"channel_id": CH, "ts": ROOT}],
    )


async def test_an_update_left_with_nothing_to_change_is_refused(
    tools: WriteTools, store: Any
) -> None:
    """ "Make Alice the owner" on a filed issue, with Alice's link unverified (#325 review)."""
    await store.link_identity("U0ALICE", "acme-alice")  # back to self-declared
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    with pytest.raises(ToolError, match=r"nothing is left to update: acme-alice can't be assigned"):
        await tools.propose_update(DRAFT, owner_update(topic_id))
    assert await store.thread_proposals(CH, ROOT) == []
    # With another change, the update goes ahead without them.
    result: Any = await tools.propose_update(
        DRAFT, owner_update(topic_id, comment="Alice owns it.")
    )
    assert result.left_off_assignees == ["acme-alice"]
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and record.proposal.changes.add_assignees == []


async def test_a_volunteer_needs_no_verified_link(tools: WriteTools, store: Any) -> None:
    link = await store.get_identity("U0BOB")
    assert link is not None and not link.verified  # they spoke for themselves
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(DRAFT, asked(topic_id, "acme-bob", "volunteered"))
    assert result.proposal_id


async def test_an_assignee_from_before_reasons_stands_in_a_revision(
    tools: WriteTools, store: Any
) -> None:
    """A draft made before #325 has assignees and no reasons; a revision may keep them."""
    from jotflow.whiteboard.write_tools import RevisedIssue, ReviseProposalArgs

    topic_id = await new_topic(tools)
    first: Any = await tools.propose_issue(DRAFT, asked(topic_id, "acme-bob", "volunteered"))
    found = await store.get_proposal(first.proposal_id)
    assert found is not None and isinstance(found.proposal, IssueProposal)
    old = found.proposal.model_copy(update={"assignee_reasons": []})  # as stored before #325
    await store.edit_proposal(found.id, old, by="U0ALICE", field="assign")
    current = await store.get_proposal(found.id)
    assert current is not None
    revised = ReviseProposalArgs(
        proposal_id=found.id,
        base_revision=current.revision,
        issue=RevisedIssue(
            title="Export the dashboard table as CSV",
            body="People want a CSV export, monthly.",
            labels=["enhancement"],
            assignees=["acme-bob"],
        ),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )
    result: Any = await tools.revise_proposal(DRAFT, revised)
    assert result.revised


async def test_a_revision_leaves_an_unverified_nomination_off_too(
    tools: WriteTools, store: Any
) -> None:
    from jotflow.whiteboard.write_tools import RevisedIssue, ReviseProposalArgs

    await store.link_identity("U0ALICE", "acme-alice")  # self-declared
    topic_id = await new_topic(tools)
    first: Any = await tools.propose_issue(DRAFT, asked(topic_id, "acme-bob", "volunteered"))
    current = await store.get_proposal(first.proposal_id)
    assert current is not None and isinstance(current.proposal, IssueProposal)
    bob = current.proposal.assignee_reasons[0]
    alice = AssigneeReason(login="acme-alice", why="named_owner", source_ts=ROOT)
    revised = ReviseProposalArgs(
        proposal_id=current.id,
        base_revision=current.revision,
        issue=RevisedIssue(
            title="Export the dashboard table as CSV",
            body="People want a CSV export, monthly.",
            labels=["enhancement"],
            assignees=["acme-bob", "acme-alice"],
            assignee_reasons=[bob, alice],
        ),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )
    result: Any = await tools.revise_proposal(DRAFT, revised)
    assert result.revised and result.left_off_assignees == ["acme-alice"]
    after = await store.get_proposal(current.id)
    assert after is not None and after.proposal.assignees == ["acme-bob"]
