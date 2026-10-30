"""A shared file, split in two, revised, then unsplit by a person's skip (#380).

Features met: attachments (#333), the split (#335), revisions (#304), a person's
skip and its undo (#138), and a new draft after the split is gone. It pins #374
and #375.
"""

from __future__ import annotations

from typing import Any

from jotflow.models import IssueProposal, ProposalState
from jotflow.store import Store
from tests.scenarios.harness import BOB, CAROL, CH, DAVE, POLICY, Clock, Scenario

ATTACHING = POLICY.replace(
    "debounce_seconds = 60\n", 'debounce_seconds = 60\nattachments = "text"\n'
)
MIXER, LABELS = "Mixer cleaning cycle", "Dashboard labels"
NOTES = (
    "Plant notes\n\n1. The mixer's cleaning cycle shows twice.\n"
    "2. The dashboard labels are too small to read from the floor.\n"
)
PENDING, SUPERSEDED = ProposalState.PENDING, ProposalState.SUPERSEDED


def body(text: str) -> str:
    return f"## Problem\n{text}\n\n## Proposal\n{text}\n\n## Acceptance\n- [ ] Done\n"


def issue(topic_id: str, title: str, *sources: str, **fields: Any) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "repo": "acme-co/web",
        "title": title,
        "body": body(title),
        "labels": ["enhancement"],
        "assignees": [],
        "sources": [{"channel_id": CH, "ts": ts} for ts in sources],
        "confidence": 0.8,
        **fields,
    }


def link(ts: str, topic_id: str) -> dict[str, Any]:
    return {"ts": ts, "decision": "existing_topic", "topic_id": topic_id, "confidence": 0.9}


async def test_a_shared_file_split_revised_and_unsplit(store: Store, clock: Clock) -> None:
    s = await Scenario(store, clock, ATTACHING).setup()

    # 1. Carol opens a thread for the plant visit, then shares the plant's notes in
    # it. The run splits the notes in two. The file goes on one topic's card only:
    # the other quotes its own part (#374).
    root = await s.say(CAROL, "Follow-ups from today's plant visit, in this thread.")
    notes = await s.say(
        CAROL,
        "Two things from the plant, notes attached.",
        thread=root,
        files=[("F0NOTES1", "notes.txt", NOTES)],
    )
    assert await s.quiet() == [root]
    run = await s.run(root)
    (shared,) = run.message(notes).files
    assert shared.file_id == "F0NOTES1" and "cleaning cycle" in shared.text.text
    await run.call("link_message", {"ts": root, "decision": "chatter", "confidence": 0.9})
    new = {"ts": notes, "decision": "new_topic", "title": MIXER, "confidence": 0.9}
    mixer = (await run.call("link_message", new)).topic_id
    also = {"ts": notes, "decision": "also_topic", "title": LABELS, "confidence": 0.9}
    labels = (await run.call("link_message", also)).also_topics[-1]
    first: Any = await run.call(
        "propose_issue",
        issue(mixer, "Show one mixer cleaning cycle", notes, attachments=["F0NOTES1"]),
    )
    twice = issue(labels, "Make the dashboard labels bigger", notes, attachments=["F0NOTES1"])
    assert "is already attached to another topic's proposal" in await run.refused(
        "propose_issue", twice
    )
    second: Any = await run.call(
        "propose_issue", issue(labels, "Make the dashboard labels bigger", notes)
    )
    await run.end()
    mixer_card, labels_card = first.proposal_id, second.proposal_id
    assert await s.states(root) == {mixer_card: PENDING, labels_card: PENDING}
    assert "notes.txt" in await s.card(mixer_card)
    assert "notes.txt" not in await s.card(labels_card)

    # 2. Dave adds to the labels part, and that card alone is revised.
    reply = await s.say(DAVE, "For the labels, at least twice the size.", thread=root)
    assert await s.quiet() == [root]
    run = await s.run(root)
    await run.call("link_message", link(reply, labels))
    pending = run.topic(LABELS).pending
    await run.call(
        "revise_proposal",
        {
            "proposal_id": pending.proposal_id,
            "base_revision": pending.revision,
            "issue": {
                "title": "Make the dashboard labels twice as big",
                "body": body("Labels at least twice the size."),
                "labels": ["enhancement"],
                "assignees": [],
            },
            "sources": [{"channel_id": CH, "ts": reply}],
            "confidence": 0.85,
        },
    )
    await run.end()
    assert (await s.proposal(labels_card)).revision == 2
    assert (await s.proposal(mixer_card)).revision == 1
    assert "Revised with new messages from Dave" in await s.card(labels_card)
    assert "notes.txt" in await s.card(mixer_card)

    # 3. Carol marks her notes as not an idea. The split is gone: the mixer topic is
    # left with no message, so its card goes, and the labels card keeps Dave's reply.
    await s.react(CAROL, "no_entry_sign", notes)
    assert await s.states(root) == {mixer_card: SUPERSEDED, labels_card: PENDING}
    dropped = await s.card(mixer_card)
    assert "Dropped." in dropped and "The drafted text has been cleared." in dropped
    assert "twice as big" in await s.card(labels_card)

    # 4. Bob says the mixer still matters. Its new card doesn't replace the labels
    # card, whose topic still has Dave's reply in the thread (#375).
    again = await s.say(BOB, "The mixer cycle still shows twice, though.", thread=root)
    assert await s.quiet() == [root]
    run = await s.run(root)
    assert run.topic(LABELS).pending.proposal_id == labels_card
    assert run.topic(MIXER).pending is None
    await run.call("link_message", link(again, mixer))
    third: Any = await run.call(
        "propose_issue", issue(mixer, "Show one mixer cleaning cycle", again)
    )
    await run.end()
    assert third.superseded == []
    assert await s.states(root) == {
        mixer_card: SUPERSEDED,
        labels_card: PENDING,
        third.proposal_id: PENDING,
    }
    new_card = await s.proposal(third.proposal_id)
    assert isinstance(new_card.proposal, IssueProposal) and new_card.proposal.attached == []
    assert "Show one mixer cleaning cycle" in await s.card(third.proposal_id)
    assert "twice as big" in await s.card(labels_card)

    # 5. Carol takes her skip back. The thread drafts again, with the notes and their
    # file, and the new mixer card is revised from them.
    await s.react(CAROL, "no_entry_sign", notes, removed=True)
    assert await s.quiet() == [root]
    run = await s.run(root)
    assert run.message(notes).files
    await run.call("link_message", link(notes, mixer))
    attach = {
        "proposal_id": third.proposal_id,
        "base_revision": run.topic(MIXER).pending.revision,
        "issue": {
            "title": "Show one mixer cleaning cycle",
            "body": body("Shown twice."),
            "labels": ["enhancement"],
            "assignees": [],
        },
        "sources": [{"channel_id": CH, "ts": notes}],
        "confidence": 0.85,
    }
    await run.call("revise_proposal", attach)
    await run.end()
    assert await s.states(root) == {
        mixer_card: SUPERSEDED,
        labels_card: PENDING,
        third.proposal_id: PENDING,
    }
    assert "Revised with new messages from Carol" in await s.card(third.proposal_id)
