"""The weekly summary of a channel's ideas (#308)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import (
    ChannelMode,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
)
from jotflow.orchestrator import summary as module
from jotflow.orchestrator.summary import (
    ACTION_NOT_THIS_WEEK,
    DEFERRED,
    MAX_ITEMS,
    NOT_THIS_SUMMARY,
    WeeklySummaries,
    deferred_blocks,
    latest_slot,
    next_slot,
)
from jotflow.policy import ChannelSummary, PolicyError, parse_policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore, SqliteStore

CH = "C0EXAMPLE1"
LONDON = ChannelSummary(day="monday", time="09:30", timezone="Europe/London")
# Monday 26 Oct 2026, 09:30 in London is 09:30 UTC (GMT, after the fall-back).
SLOT = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)


def policy(summary: str = 'day = "monday"\ntime = "09:30"\ntimezone = "Europe/London"') -> Any:
    return parse_policy(
        f'[channels.{CH}]\n\n[[channels.{CH}.repos]]\nrepo = "acme-co/web"\n'
        f'nickname = "web"\ndescription = "Web"\n\n[channels.{CH}.summary]\n{summary}\n'
    )


# --- the clock ------------------------------------------------------------------------------


def test_the_latest_slot_is_this_weeks_once_it_has_passed() -> None:
    assert latest_slot(SLOT, LONDON) == SLOT
    assert latest_slot(SLOT + timedelta(days=2), LONDON) == SLOT
    # A minute before Monday's slot: last week's, which was in summer time (BST).
    assert latest_slot(SLOT - timedelta(minutes=1), LONDON) == datetime(
        2026, 10, 19, 8, 30, tzinfo=UTC
    )
    assert next_slot(SLOT, LONDON) == datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


def test_a_time_lost_to_spring_forward_comes_an_hour_later() -> None:
    # London skips 01:00 to 02:00 on Sunday 29 Mar 2026.
    gap = ChannelSummary(day="sunday", time="01:30", timezone="Europe/London")
    slot = latest_slot(datetime(2026, 3, 29, 12, 0, tzinfo=UTC), gap)
    assert slot == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # 02:30 BST, an hour later


def test_a_time_that_happens_twice_is_the_first() -> None:
    # London repeats 01:00 to 02:00 on Sunday 25 Oct 2026.
    twice = ChannelSummary(day="sunday", time="01:30", timezone="Europe/London")
    slot = latest_slot(datetime(2026, 10, 25, 12, 0, tzinfo=UTC), twice)
    assert slot == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 01:30 BST, the first


@pytest.mark.parametrize(
    "bad",
    [
        'day = "monday"\ntime = "9:30"\ntimezone = "Europe/London"',
        'day = "monday"\ntime = "24:00"\ntimezone = "Europe/London"',
        'day = "mon"\ntime = "09:30"\ntimezone = "Europe/London"',
        'day = "monday"\ntime = "09:30"\ntimezone = "Mars/Olympus"',
        'day = "monday"\ntime = "09:30"\ntimezone = "../etc/passwd"',
        'day = "monday"\ntime = "09:30"',
    ],
)
def test_bad_settings_are_refused_at_load(bad: str) -> None:
    with pytest.raises(PolicyError):
        policy(bad)


def test_setup_keeps_the_summary() -> None:
    settings = policy()
    assert parse_policy(render_policy(settings)).channels[CH].summary == LONDON


# --- what's in it ---------------------------------------------------------------------------


class People:
    """Slack, as the summary sees it: who wrote each message, and their names."""

    def __init__(self, deleted: set[str] | None = None) -> None:
        self.deleted = deleted or set()

    async def authors(self, refs: Any) -> set[str]:
        found: set[str] = set()
        for ref in refs:
            if ref.ts in self.deleted:
                raise LookupError("gone")
            found.add("U0SAM" if ref.ts == ref.thread_ts or ref.thread_ts is None else "U0KIM")
        return found

    async def name(self, user_id: str) -> str | None:
        return {"U0SAM": "Sam", "U0KIM": "<@U0KIM> Kim"}.get(user_id)

    async def workspace_url(self) -> str | None:
        return "https://acme-co.slack.com/"


class Slack:
    def __init__(self, error: str | None = None) -> None:
        self.posts: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.error = error

    async def post(self, channel_id: str, text: str, blocks: list[dict[str, Any]]) -> str:
        if self.error:
            raise _SlackError(self.error)
        for block in blocks:  # Slack refuses a repeated action_id within one block
            ids = [e.get("action_id") for e in block.get("elements", []) if "action_id" in e]
            if len(ids) != len(set(ids)):
                raise _SlackError("invalid_blocks")
        self.posts.append((channel_id, text, blocks))
        return f"1793000000.{len(self.posts):06d}"

    async def update(self, channel_id: str, ts: str, text: str, blocks: Any) -> None:
        pass

    async def permalink(self, channel_id: str, ts: str) -> str | None:
        return f"https://acme-co.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


class _SlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = type("R", (), {"data": {"error": error}})()


async def idea(
    store: Any,
    n: int,
    *,
    title: str = "Export the dashboard",
    when: datetime = SLOT - timedelta(days=2),
    state: ProposalState = ProposalState.PENDING,
    cleared: bool = False,
) -> tuple[str, str]:
    """One topic with one create proposal in its own thread. Returns (topic, proposal)."""
    thread = f"179{n:07d}.000100"
    topic = (await store.create_topic(CH, f"Topic {n}")).topic
    root = MessageRef(channel_id=CH, ts=thread)
    await store.link_message(root, topic.id)
    record = ProposalRecord(
        id=f"p{n:015d}",
        channel_id=CH,
        thread_ts=thread,
        run_id="r1",
        proposal=IssueProposal(
            topic_id=topic.id,
            repo="acme-co/web",
            title=title,
            body="b",
            confidence=0.9,
            sources=[root],
        ),
        created_at=when,
    )
    await store.add_proposal(record)
    await store.set_card_ts(record.id, f"{thread[:-6]}000900")
    await reach(store, record.id, state)
    if cleared:
        await store.clear_draft(record.id)
    return topic.id, record.id


async def reach(store: Any, proposal_id: str, state: ProposalState) -> None:
    """Walk a new proposal to ``state`` the way the app would."""
    from jotflow.models import ExecutionResult
    from jotflow.store import AUTO_APPROVER

    if state in (ProposalState.REJECTED, ProposalState.SUPERSEDED):
        await store.transition(proposal_id, state)
    elif state in (ProposalState.APPLIED, ProposalState.FAILED):
        await store.transition(proposal_id, ProposalState.APPROVED, approved_by=AUTO_APPROVER)
        await store.transition(proposal_id, ProposalState.EXECUTING)
        result = (
            ExecutionResult(issue_url="https://github.com/acme-co/web/issues/42")
            if state is ProposalState.APPLIED
            else ExecutionResult(error="GitHub refused")
        )
        await store.transition(proposal_id, state, result=result)


def summaries(store: Any, slack: Slack, now: datetime, people: People | None = None) -> Any:
    return WeeklySummaries(store, policy(), slack, people or People(), clock=lambda: now)


def texts(blocks: list[dict[str, Any]]) -> str:
    return str(blocks)


async def test_it_posts_once_a_week_in_plain_text_with_names_and_links() -> None:
    store = MemoryStore()
    await idea(store, 1, title="<!channel> *Export* the dashboard")
    slack = Slack()
    run = summaries(store, slack, SLOT + timedelta(seconds=30))
    assert await run.tick() == [CH]
    assert await run.tick() == []  # posted already
    [(channel, _, blocks)] = slack.posts
    assert channel == CH
    item = next(b for b in blocks if str(b.get("block_id", "")).startswith("item:"))
    assert item["text"]["type"] == "plain_text"  # nothing in it is markup or a mention
    body = item["text"]["text"]
    assert "<!channel>" not in body and "*" not in body
    assert "Suggested by Sam" in body and "Waiting for approval" in body
    buttons = next(b for b in blocks if b.get("type") == "actions")["elements"]
    labels = [b["text"]["text"] for b in buttons]
    assert labels == ["Thread", "Open card", "Not this week"]
    assert buttons[-1]["action_id"] == ACTION_NOT_THIS_WEEK


async def test_a_restart_never_posts_the_same_week_twice(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    slack = Slack()
    first = await SqliteStore.open(db)
    try:
        await idea(first, 1)
        assert await summaries(first, slack, SLOT).tick() == [CH]
    finally:
        await first.close()
    again = await SqliteStore.open(db)
    try:
        assert await summaries(again, slack, SLOT + timedelta(minutes=3)).tick() == []
    finally:
        await again.close()
    assert len(slack.posts) == 1


@pytest.mark.parametrize(
    ("state", "cleared"),
    [
        (ProposalState.REJECTED, False),
        (ProposalState.SUPERSEDED, False),
        (ProposalState.FAILED, True),  # its draft text was cleared
    ],
)
async def test_nothing_removed_is_posted_again(state: ProposalState, cleared: bool) -> None:
    store = MemoryStore()
    await idea(store, 1, title="Kept idea")
    await idea(store, 2, title="Removed idea", state=state, cleared=cleared)
    slack = Slack()
    await summaries(store, slack, SLOT).tick()
    text = texts(slack.posts[0][2])
    assert "Kept idea" in text and "Removed idea" not in text


async def test_an_idea_whose_messages_were_all_deleted_is_left_out() -> None:
    store = MemoryStore()
    await idea(store, 1, title="Deleted idea")
    slack = Slack()
    await summaries(store, slack, SLOT, People(deleted={"1790000001.000100"})).tick()
    assert "Deleted idea" not in texts(slack.posts[0][2])


async def test_only_the_windows_ideas_and_waiting_ones_first() -> None:
    store = MemoryStore()
    await idea(store, 1, title="Old idea", when=SLOT - timedelta(days=9))
    filed, _ = await idea(store, 2, title="Filed idea", state=ProposalState.APPLIED)
    await store.rename_topic(filed, "Filed idea")  # filing clears the draft: the topic names it
    await idea(store, 3, title="Waiting idea", when=SLOT - timedelta(days=5))
    slack = Slack()
    await summaries(store, slack, SLOT).tick()
    text = texts(slack.posts[0][2])
    assert "Old idea" not in text
    assert text.index("Waiting idea") < text.index("Filed idea")
    assert "'Issue'" in text and "issues/42" in text and "Filed as web#42" in text


async def test_a_busy_week_stays_within_slacks_limits() -> None:
    store = MemoryStore()
    for n in range(MAX_ITEMS + 5):
        await idea(store, n, title=f"Idea {n} " + "x" * 200)
    slack = Slack()
    await summaries(store, slack, SLOT).tick()
    blocks = slack.posts[0][2]
    assert len(blocks) <= 50
    assert sum(1 for b in blocks if str(b.get("block_id", "")).startswith("item:")) == MAX_ITEMS
    assert "And 5 more" in texts(blocks)
    item = next(b for b in blocks if str(b.get("block_id", "")).startswith("item:"))
    assert len(item["text"]["text"].splitlines()[0]) <= 80  # the card's title limit


async def test_a_paused_channel_gets_no_summary_that_week() -> None:
    store = MemoryStore()
    await idea(store, 1)
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0ADMIN")
    slack = Slack()
    assert await summaries(store, slack, SLOT).tick() == []
    await store.set_channel_mode(CH, ChannelMode.PROPOSE, changed_by="U0ADMIN")
    assert await summaries(store, slack, SLOT + timedelta(minutes=5)).tick() == []  # skipped
    assert slack.posts == []


async def test_a_slot_missed_by_too_long_is_skipped_and_a_late_one_says_so() -> None:
    store = MemoryStore()
    slack = Slack()
    assert await summaries(store, slack, SLOT + timedelta(hours=13)).tick() == []
    assert slack.posts == []
    later = SLOT + timedelta(days=7, hours=1)  # next week's, an hour late
    assert await summaries(store, slack, later).tick() == [CH]
    assert "posted late" in texts(slack.posts[0][2])


async def test_a_channel_the_bot_left_is_skipped_quietly_and_others_retry() -> None:
    store = MemoryStore()
    assert await summaries(store, Slack("not_in_channel"), SLOT).tick() == []
    post = await store.get_summary(CH, SLOT)
    assert post is not None and post.status == "skipped"
    other = MemoryStore()
    assert await summaries(other, Slack("ratelimited"), SLOT).tick() == []
    slack = Slack()
    assert await summaries(other, slack, SLOT + timedelta(minutes=1)).tick() == [CH]


async def test_monkeypatched_limits_are_read_at_run_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_ITEMS", 1)
    store = MemoryStore()
    await idea(store, 1)
    await idea(store, 2)
    slack = Slack()
    await summaries(store, slack, SLOT).tick()
    assert "And 1 more" in texts(slack.posts[0][2])


# --- Not this week --------------------------------------------------------------------------


async def test_not_this_week_puts_the_topic_off_to_the_next_summary() -> None:
    store = MemoryStore()
    topic, _ = await idea(store, 1, title="Put off")
    slack = Slack()
    run = summaries(store, slack, SLOT)
    await run.tick()
    ts = "1793000000.000001"
    value = f"{topic}|{SLOT.isoformat()}"
    assert await run.defer("U0ALEX", CH, "1793000000.999999", value) == NOT_THIS_SUMMARY
    assert await run.defer("U0ALEX", CH, ts, f"{topic}|nonsense") == NOT_THIS_SUMMARY
    assert await run.defer("U0ALEX", CH, ts, value) == DEFERRED
    upcoming = next_slot(SLOT, LONDON)
    [deferral] = await store.deferred_topics(CH, upcoming)
    assert (deferral.topic_id, deferral.by) == (topic, "U0ALEX")
    # Next week it's listed again, marked, even though nothing new happened.
    later = summaries(store, slack, upcoming)
    assert await later.tick() == [CH]
    assert "Put off from last week" in texts(slack.posts[-1][2])


def test_the_summary_shows_who_put_an_item_off() -> None:
    blocks = [
        {"type": "section", "block_id": "item:t1", "text": {"type": "plain_text", "text": "x"}},
        {"type": "actions", "block_id": "actions:t1", "elements": []},
        {"type": "actions", "block_id": "actions:t2", "elements": []},
    ]
    out = deferred_blocks(blocks, "t1", "Alex")
    assert out[1]["type"] == "context" and "Put off to next week by Alex" in str(out[1])
    assert out[2]["type"] == "actions"


async def test_not_this_week_follows_approves_rules() -> None:
    from jotflow.orchestrator.approval import ApprovalHandler
    from tests.test_approval import BOB, TEAM
    from tests.test_approval import CH as GATE_CH
    from tests.test_approval import Slack as GateSlack
    from tests.test_approval import policy as gate_policy

    store = MemoryStore()
    seen: list[str] = []

    async def act(click: Any) -> str:
        seen.append(click.user_id)
        return "done"

    def body(user: str) -> dict[str, Any]:
        return {
            "user": {"id": user, "team_id": TEAM},
            "team": {"id": TEAM},
            "channel": {"id": GATE_CH},
            "container": {"message_ts": "1793000000.000001"},
            "actions": [{"action_id": ACTION_NOT_THIS_WEEK, "value": "t1|x"}],
        }

    open_gate = ApprovalHandler(store, gate_policy(None), GateSlack())
    assert await open_gate.checked_click(body(BOB), act) == "done"
    approvers = ApprovalHandler(store, gate_policy(["U0ALICE"]), GateSlack())
    assert await approvers.checked_click(body(BOB), act) == "refused"
    await store.set_channel_mode(GATE_CH, ChannelMode.PAUSED, changed_by="U0ADMIN")
    assert await open_gate.checked_click(body(BOB), act) == "refused"
    assert seen == [BOB]


async def test_a_filed_idea_whose_messages_were_all_deleted_is_left_out_too() -> None:
    store = MemoryStore()
    filed, _ = await idea(store, 1, state=ProposalState.APPLIED)
    await store.rename_topic(filed, "Filed and deleted")
    slack = Slack()
    await summaries(store, slack, SLOT, People(deleted={"1790000001.000100"})).tick()
    assert "Filed and deleted" not in texts(slack.posts[0][2])


def test_every_link_button_has_its_own_action_id_and_one_pattern_answers_them() -> None:
    from jotflow.orchestrator.summary import ACTION_LINK_PATTERN, _link

    ids = [_link(label, "https://x")["action_id"] for label in ("Thread", "Issue", "Open card")]
    assert len(set(ids)) == 3
    assert all(ACTION_LINK_PATTERN.match(i) for i in ids)
    assert not ACTION_LINK_PATTERN.match(ACTION_NOT_THIS_WEEK)
