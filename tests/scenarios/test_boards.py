"""One idea's whole way to approval: a board, a priority and an owner named in the
post, a found board picked on the card, a revision, and the approval (#380).

Features met: listed and found boards (#309), the card's board menu (#327), a board
and an owner named in words (#331), a stated priority (#332), the triage guide
(#317), the [issues] label and title prefix (#306), revisions (#304), and approval
with its hash and locked settings (#152, #306). It covers #376's naming; its
nickname clash is pinned with that fix.
"""

from __future__ import annotations

from typing import Any

from jotflow.models import FoundBoard, IssueProposal, ProposalState
from jotflow.store import Store
from tests.scenarios.harness import ALICE, BOB, CAROL, CH, DAVE, Clock, Scenario

BOARDED = f"""
[issues]
label = "from-slack"
title_prefix = "[Slack]"

[channels.{CH}]
debounce_seconds = 60
triage_guide = "Sign-in and layout work goes to web. Anything urgent gets its priority."
priority_labels = {{ urgent = "priority: high" }}

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app: dashboard, exports and sign-in"
labels = ["bug", "enhancement", "priority: high", "from-slack"]
owner_hints = ["{BOB}"]

[[channels.{CH}.boards]]
board = "acme-co/3"
nickname = "roadmap"
description = "Planned work for the next two quarters"

[[channels.{CH}.board_scopes]]
owner = "acme-co"
numbers = [12, 14]
description = "Each team's own board"
aliases = {{ design = 12 }}
"""
FOUND = [
    FoundBoard(number=12, title="Design team"),
    FoundBoard(number=14, title="Ops board"),
]


def body(text: str) -> str:
    return f"## Problem\n{text}\n\n## Proposal\n{text}\n\n## Acceptance\n- [ ] Done\n"


def boards_of(record: Any) -> list[tuple[str, str]]:
    assert isinstance(record.proposal, IssueProposal)
    return [(b.board, b.reason) for b in record.proposal.boards]


async def test_boards_named_picked_revised_and_approved(store: Store, clock: Clock) -> None:
    s = await Scenario(store, clock, BOARDED, found=FOUND).setup()

    # 1. Carol's post names a board by its alias, a priority and an owner in words.
    # The packet has the triage guide, the priority map and every board.
    idea = await s.say(
        CAROL,
        "The sign-in page needs a new layout. It's urgent. Put it on the design board, "
        "and Bob should own it.",
    )
    assert await s.quiet() == [idea]
    run = await s.run(idea)
    channel = run.work.conversation.channel  # type: ignore[union-attr]
    assert channel is not None and channel.triage_guide is not None
    (web,) = [r for r in channel.repos if r.repo == "acme-co/web"]
    assert web.priority_labels == {"urgent": "priority: high"}
    offered = {b.nickname for b in channel.boards}
    assert {"roadmap", "design"} <= offered and len(offered) == 3
    ops = next(n for n in offered if n not in ("roadmap", "design"))
    new = {"ts": idea, "decision": "new_topic", "title": "Sign-in layout", "confidence": 0.9}
    topic_id = (await run.call("link_message", new)).topic_id
    made: Any = await run.call(
        "propose_issue",
        {
            "topic_id": topic_id,
            "repo": "acme-co/web",
            "title": "A new sign-in page layout",
            "body": body("The sign-in page needs a new layout."),
            "labels": ["enhancement", "priority: high"],
            "assignees": ["acme-bob"],
            "assignee_reasons": [{"login": "acme-bob", "why": "named_in_text", "source_ts": idea}],
            "boards": [{"board": "design", "reason": "Carol asked for the design board"}],
            "sources": [{"channel_id": CH, "ts": idea}],
            "confidence": 0.8,
        },
    )
    await run.end()
    card_id = made.proposal_id
    record = await s.proposal(card_id)
    assert [b for b, _ in boards_of(record)] == ["acme-co/12"]
    card = await s.card(card_id)
    assert "[Slack] A new sign-in page layout" in card
    assert "from-slack" in card and "priority: high" in card
    assert "named in the thread ('Bob')" in card
    assert "Add to boards: design" in card
    assert (
        "The agent's reason for design (acme-co/12), found by scope: "
        "Carol asked for the design board"
    ) in card

    # 2. Bob adds the ops board on the card's menu, keeping design.
    assert await s.pick_boards(card_id, BOB, "design", ops) == "edited"
    record = await s.proposal(card_id)
    assert [b for b, _ in boards_of(record)] == ["acme-co/12", "acme-co/14"]
    card = await s.card(card_id)
    assert f"{ops} (acme-co/14), found by scope was added on the card." in card
    assert "The agent's reason for design" in card

    # 3. Dave adds to the idea. The revision keeps both boards and their reasons.
    reply = await s.say(DAVE, "It must work with password managers too.", thread=idea)
    assert await s.quiet() == [idea]
    run = await s.run(idea)
    pending = run.topic("Sign-in layout").pending
    joined = {"ts": reply, "decision": "existing_topic", "topic_id": topic_id, "confidence": 0.9}
    await run.call("link_message", joined)
    await run.call(
        "revise_proposal",
        {
            "proposal_id": pending.proposal_id,
            "base_revision": pending.revision,
            "issue": {
                "title": "A new sign-in page layout that works with password managers",
                "body": body("A new layout that works with password managers."),
                "labels": ["enhancement", "priority: high"],
                "assignees": list(pending.assignees),
                "assignee_reasons": [r.model_dump() for r in pending.assignee_reasons],
            },
            "sources": [{"channel_id": CH, "ts": reply}],
            "confidence": 0.85,
        },
    )
    await run.end()
    revised = await s.proposal(card_id)
    assert boards_of(revised) == boards_of(record)
    card = await s.card(card_id)
    assert (
        "password managers" in card
        and f"{ops} (acme-co/14), found by scope was added on the card." in card
    )
    assert "Revised with new messages from Dave" in card

    # 4. Alice approves what the card shows. The boards, the owner, the priority and
    # the [issues] settings the card showed are locked in.
    assert await s.approve(card_id, ALICE) == "approved"
    approved = await s.proposal(card_id)
    assert approved.state is ProposalState.APPROVED
    assert [b for b, _ in boards_of(approved)] == ["acme-co/12", "acme-co/14"]
    assert isinstance(approved.proposal, IssueProposal)
    assert approved.proposal.assignees == ["acme-bob"]
    assert "priority: high" in approved.proposal.labels
    assert approved.approved_issues is not None
    assert "from-slack" in approved.approved_issues and "[Slack]" in approved.approved_issues
