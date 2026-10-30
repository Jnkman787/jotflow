"""A pasted email, split, then changed on one card, Add me on the other, and a
quick follow-up post (#380).

Features met: the split (#335), Request Changes (#330), Add me (#305), a
same-author follow-up (#334), revisions (#304). It pins #374, #375 and #378.
"""

from __future__ import annotations

from typing import Any

from jotflow.models import IssueProposal, ProposalRecord, ProposalState
from jotflow.store import Store
from tests.scenarios.harness import ALICE, BOB, CAROL, CH, Clock, Scenario

MIXER, SENSOR = "Mixer cleaning cycle", "Sensor reading timing"
PENDING = ProposalState.PENDING

EMAIL = (
    "Forwarding the plant's email: 1. the dashboard shows the mixer's cleaning cycle "
    "twice. 2. sensor readings arrive late; they should come every second."
)


def new_topic(ts: str, title: str) -> dict[str, Any]:
    return {"ts": ts, "decision": "new_topic", "title": title, "confidence": 0.9}


def assignees(record: ProposalRecord) -> list[str]:
    assert isinstance(record.proposal, IssueProposal)
    return list(record.proposal.assignees)


def body(problem: str, proposal: str) -> str:
    return (
        f"## Problem\n{problem}\n\n## Proposal\n{proposal}\n\n"
        "## Acceptance\n- [ ] It works as described\n"
    )


def issue(topic_id: str, repo: str, title: str, text: str, *sources: str) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "repo": repo,
        "title": title,
        "body": body(text, text),
        "labels": ["enhancement"],
        "assignees": [],
        "sources": [{"channel_id": CH, "ts": ts} for ts in sources],
        "confidence": 0.8,
    }


def revision(pending: Any, title: str, text: str, *sources: str, **issue: Any) -> dict[str, Any]:
    return {
        "proposal_id": pending.proposal_id,
        "base_revision": pending.revision,
        "issue": {
            "title": title,
            "body": body(text, text),
            "labels": ["enhancement"],
            "assignees": list(pending.assignees),
            **issue,
        },
        "sources": [{"channel_id": CH, "ts": ts} for ts in sources],
        "confidence": 0.85,
    }


async def test_a_split_email_followed_up_changed_and_added_to(store: Store, clock: Clock) -> None:
    s = await Scenario(store, clock).setup()

    # 1. Carol pastes an email with two actions, and adds to it a moment later. The
    # follow-up's run waits for the paste's (#334), which splits it in two cards.
    paste = await s.say(CAROL, EMAIL)
    follow = await s.say(CAROL, "Also: the cleaning cycle label should read Cleaning.")
    assert await s.quiet() == [paste]
    run = await s.run(paste)
    mixer = (await run.call("link_message", new_topic(paste, MIXER))).topic_id
    also = {"ts": paste, "decision": "also_topic", "title": SENSOR, "confidence": 0.9}
    sensor = (await run.call("link_message", also)).also_topics[-1]
    first: Any = await run.call(
        "propose_issue",
        issue(mixer, "acme-co/web", "Show one mixer cleaning cycle", "Shown twice.", paste),
    )
    second: Any = await run.call(
        "propose_issue",
        issue(sensor, "acme-co/sensors", "Send sensor readings every second", "Late.", paste),
    )
    await run.end()
    mixer_card, sensor_card = first.proposal_id, second.proposal_id
    assert await s.states(paste) == {mixer_card: PENDING, sensor_card: PENDING}
    assert "Show one mixer cleaning cycle" in await s.card(mixer_card)
    assert "Send sensor readings every second" in await s.card(sensor_card)
    # Each card says who its issue will credit, and locks it (#396).
    assert "Credited in the issue: raised by Carol" in await s.card(mixer_card)
    assert (await s.proposal(mixer_card)).card_credits == '["U0CAROL"]'

    # 2. The follow-up's run sees what it follows, and each split topic's card (#374).
    assert await s.quiet() == [follow]
    run = await s.run(follow)
    hint = run.work.conversation.thread.follows  # type: ignore[union-attr]
    assert hint is not None and (hint.ts, hint.topic_id) == (paste, mixer)
    assert hint.also_topics == [sensor]
    assert run.topic(MIXER).pending.proposal_id == mixer_card
    assert run.topic(SENSOR).pending.proposal_id == sensor_card
    joined = {"ts": follow, "decision": "existing_topic", "topic_id": mixer, "confidence": 0.9}
    await run.call("link_message", joined)
    await run.call(
        "revise_proposal",
        revision(
            run.topic(MIXER).pending,
            "Show one mixer cleaning cycle, labelled Cleaning",
            "Show the cycle once, labelled Cleaning.",
            follow,
        ),
    )
    await run.end()
    assert (await s.proposal(mixer_card)).revision == 2
    assert (await s.proposal(sensor_card)).revision == 1
    card = await s.card(mixer_card)
    assert "labelled Cleaning" in card and "Revised with new messages from Carol" in card
    assert "Send sensor readings every second" in await s.card(sensor_card)

    # 3. Bob asks for a change on the sensor card. JotFlow posts it in the thread,
    # and the next run revises that card with it, and only that one (#375).
    await s.request_changes(sensor_card, BOB, "Every half second, not every second.")
    (posted,) = s.change_requests(paste)
    assert posted["metadata"]["event_payload"]["by"] == BOB
    assert await s.quiet() == [paste]
    run = await s.run(paste)
    seen = run.message(posted["ts"])
    assert (seen.change_request_by, seen.change_request_for) == (BOB, sensor_card)
    wrong = revision(run.topic(MIXER).pending, "Show one mixer cleaning cycle", "No.", posted["ts"])
    assert f"is a change request for proposal {sensor_card}" in await run.refused(
        "revise_proposal", wrong
    )
    await run.call(
        "revise_proposal",
        revision(
            run.topic(SENSOR).pending,
            "Send sensor readings every half second",
            "Readings should come every half second.",
            posted["ts"],
        ),
    )
    await run.end()
    assert await s.states(paste) == {mixer_card: PENDING, sensor_card: PENDING}
    assert (await s.proposal(mixer_card)).revision == 2
    sensor_now = await s.proposal(sensor_card)
    assert sensor_now.revision == 2 and BOB in sensor_now.participants
    card = await s.card(sensor_card)
    assert "Credited in the issue: raised by Carol, with contributions from Bob" in card
    assert "Send sensor readings every half second" in card
    assert "Revised with new messages from Bob" in card
    assert "labelled Cleaning" in await s.card(mixer_card)

    # 4. Alice adds herself on the mixer card. The sensor card is untouched.
    assert await s.add_me(mixer_card, ALICE) == "added"
    assert assignees(await s.proposal(mixer_card)) == ["acme-alice"]
    assert "added on the card" in await s.card(mixer_card)
    assert assignees(await s.proposal(sensor_card)) == []

    # 5. Bob replies with one more change. The run sees why Alice is on the card, and
    # when the agent leaves her out, a person's add stays all the same (#378).
    reply = await s.say(BOB, "And make the Cleaning label bold.", thread=paste)
    assert await s.quiet() == [paste]
    run = await s.run(paste)
    pending = run.topic(MIXER).pending
    assert [(r.login, r.why) for r in pending.assignee_reasons] == [("acme-alice", "added_on_card")]
    await run.call("link_message", {**joined, "ts": reply})
    await run.call(
        "revise_proposal",
        revision(
            pending,
            "Show one mixer cleaning cycle, labelled Cleaning in bold",
            "Show the cycle once, labelled Cleaning, in bold.",
            reply,
            assignees=[],
        ),
    )
    await run.end()
    mixer_now = await s.proposal(mixer_card)
    assert mixer_now.revision == 4  # the add, then this revision
    assert assignees(mixer_now) == ["acme-alice"]
    card = await s.card(mixer_card)
    assert "in bold" in card and "added on the card" in card
    assert "Revised 2 times with new messages from Bob" in card
    assert (await s.proposal(sensor_card)).revision == 2
    assert await s.states(paste) == {mixer_card: PENDING, sensor_card: PENDING}
