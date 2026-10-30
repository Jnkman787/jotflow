"""Two ideas, the weekly summary, and replies in its thread (#380).

Features met: drafting, a message about JotFlow itself (#336), the weekly summary
(#308), `re: N` in its thread and the guard that keeps it from being drafted (#318),
a bot mention (#329) with questions off (#57), and a skip reaction added and taken
back (#138). It pins #373.
"""

from __future__ import annotations

from typing import Any

from jotflow.models import ProposalState
from jotflow.store import Store
from tests.scenarios.harness import ALICE, BOB, BOT, CAROL, CH, DAVE, POLICY, Clock, Scenario

SUMMING = (
    POLICY
    + f"""
[channels.{CH}.summary]
day = "monday"
time = "09:30"
timezone = "Europe/London"

[channels.{CH}.questions]
enabled = false
"""
)
CSV, DARK = "CSV export of reports", "Dark mode"
STILL_READ = "still read"


def issue(topic_id: str, title: str, ts: str) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "repo": "acme-co/web",
        "title": title,
        "body": f"## Problem\n{title}\n\n## Proposal\n{title}\n\n## Acceptance\n- [ ] Done\n",
        "labels": ["enhancement"],
        "assignees": [],
        "sources": [{"channel_id": CH, "ts": ts}],
        "confidence": 0.8,
    }


async def idea(s: Scenario, user: str, text: str, title: str, card: str) -> tuple[str, str, str]:
    """``user`` posts an idea, and its run proposes it. Returns (ts, topic, card id)."""
    ts = await s.say(user, text)
    assert await s.quiet() == [ts]
    run = await s.run(ts)
    new = {"ts": ts, "decision": "new_topic", "title": title, "confidence": 0.9}
    topic_id = (await run.call("link_message", new)).topic_id
    made: Any = await run.call("propose_issue", issue(topic_id, card, ts))
    await run.end()
    return ts, topic_id, made.proposal_id


async def test_replies_in_the_summarys_thread_route_and_never_draft(
    store: Store, clock: Clock
) -> None:
    s = await Scenario(store, clock, SUMMING).setup()

    # 1. Two ideas, each with its card.
    csv_ts, csv, csv_card = await idea(
        s, CAROL, "Could we export reports as CSV?", CSV, "Export CSV"
    )
    dark_ts, dark, dark_card = await idea(s, BOB, "Dark mode for the dashboard?", DARK, "Dark mode")
    assert "Export CSV" in await s.card(csv_card)

    # A question about JotFlow itself isn't an idea: its run points to help (#336).
    about = await s.say(DAVE, "Does JotFlow read every channel here?")
    assert await s.quiet() == [about]
    run = await s.run(about)
    await run.call("point_to_help", {"ts": about})
    await run.end()
    assert await s.states(about) == {}
    (pointer,) = thread_posts(s, about)
    assert "isn't drafted as an issue" in pointer["text"]

    # 2. Monday's summary lists both, numbered.
    s.clock.now = s.clock.now.replace(hour=9, minute=31)
    summary = await s.post_summary()
    listed = str(s.summary_posts.posts[summary])
    assert "Export CSV" in listed and "Dark mode" in listed and "JotFlow" not in listed
    number = (
        {CSV: 1, DARK: 2}
        if listed.index("Export CSV") < listed.index("Dark mode")
        else {
            CSV: 2,
            DARK: 1,
        }
    )

    # 3. Dave's re: in the summary's thread joins the dark mode idea and leaves a note
    # in its thread. The summary's thread itself is never drafted (#318).
    dave = await s.say(
        DAVE, f"re: {number[DARK]} it should follow the system setting.", thread=summary
    )
    link = await store.get_message(CH, dave)
    assert link is not None and link.topic_id == dark and link.linked_by == "person"
    assert any("Dave" in m.get("text", "") for m in thread_posts(s, dark_ts))
    assert summary not in await s.quiet()

    # 4. Alice mentions JotFlow before her re:, with questions off. It routes all the
    # same, and she isn't told her message is read with the thread's ideas (#373).
    told = len(s.slack.whispers)
    alice = await s.say(
        ALICE, f"<@{BOT.user_id}> re: {number[CSV]} add an Excel option too.", thread=summary
    )
    link = await store.get_message(CH, alice)
    assert link is not None and link.topic_id == csv
    assert not any(STILL_READ in text for _, text in s.slack.whispers[told:])
    assert summary not in await s.quiet()

    # 5. Bob's plain reply is pointed to re: once. His skip, added and taken back,
    # drafts nothing in the summary's thread (#373).
    bob = await s.say(BOB, "Nice list.", thread=summary)
    assert any(user == BOB for user, _ in s.slack.whispers[told:])
    await s.react(BOB, "no_entry_sign", bob)
    await s.react(BOB, "no_entry_sign", bob, removed=True)
    # Slack's reaction names no thread, so the undo queues a run for Bob's reply. It
    # is served nothing to draft, so nothing can be linked or proposed there.
    assert await s.quiet() == [bob]
    run = await s.run(bob)
    thread = run.work.conversation.thread  # type: ignore[union-attr]
    assert thread.messages == [] and thread.nothing_to_draft is not None
    await run.end()

    # Nothing in the summary's thread became a card, and both ideas still wait.
    assert await s.states(summary) == {}
    assert await s.states(csv_ts) == {csv_card: ProposalState.PENDING}
    assert await s.states(dark_ts) == {dark_card: ProposalState.PENDING}


def thread_posts(s: Scenario, thread: str) -> list[dict[str, Any]]:
    return [m for m in s.slack.messages.values() if m.get("thread_ts") == thread]
