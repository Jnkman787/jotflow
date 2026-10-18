"""One contract, two stores: every test runs against MemoryStore and SqliteStore."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    IssueChanges,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    UpdateProposal,
)
from jotflow.store import (
    ALLOWED_TRANSITIONS,
    AUTO_APPROVER,
    CLEARED_TITLE,
    FINAL_STATES,
    ConflictError,
    ContentChangedError,
    DraftClearedError,
    MemoryStore,
    NotFoundError,
    SqliteStore,
    Store,
    ToolCallRecord,
    TransitionError,
    content_hash,
    try_transition,
)

S = ProposalState
CH = "C0IDEAS01"
OTHER = "C0IDEAS02"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float = 1) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory", "sqlite"])
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, clock: Clock
) -> AsyncIterator[Store]:
    if request.param == "memory":
        made: Store = MemoryStore(clock=clock)
    else:
        made = await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)
    yield made
    await made.close()


def ref(ts: str, channel: str = CH, thread: str | None = None) -> MessageRef:
    return MessageRef(channel_id=channel, ts=ts, thread_ts=thread)


def draft(**overrides: object) -> IssueProposal:
    fields: dict[str, object] = {
        "repo": "acme-co/web",
        "title": "Onboarding progress bar",
        "body": "Add a progress bar to onboarding.",
        "labels": ["enhancement"],
        "assignees": ["octo-dev"],
        "confidence": 0.9,
    }
    fields.update(overrides)
    return IssueProposal.model_validate(fields)


def record(
    proposal_id: str = "p1",
    *,
    channel: str = CH,
    thread: str | None = "1760781600.000100",
    proposal: IssueProposal | UpdateProposal | None = None,
    participants: list[str] | None = None,
    created: datetime | None = None,
) -> ProposalRecord:
    return ProposalRecord(
        id=proposal_id,
        channel_id=channel,
        thread_ts=thread,
        run_id="run-1",
        proposal=proposal or draft(),
        participants=participants if participants is not None else ["U0ALICE", "U0BOB"],
        created_at=created or datetime(2026, 10, 18, 9, 0, tzinfo=UTC),
    )


ISSUE_URL = "https://github.com/acme-co/web/issues/42"
SHOWN = content_hash(draft())  # what the card showed


async def approved(store: Store, proposal_id: str = "p1") -> ProposalRecord:
    await store.add_proposal(record(proposal_id))
    return await store.transition(
        proposal_id, S.APPROVED, approved_by="U0CAROL", expected_hash=SHOWN
    )


# --- topics ------------------------------------------------------------------


async def test_create_and_get_topic(store: Store, clock: Clock) -> None:
    created = await store.create_topic(CH, "Onboarding progress bar")
    assert created.topic.channel_id == CH
    assert created.topic.title == "Onboarding progress bar"
    assert created.created_at == created.last_activity_at == clock.now
    fetched = await store.get_topic(created.topic.id)
    assert fetched == created
    assert await store.get_topic("missing") is None


async def test_topic_titles_are_cleaned_on_every_write(store: Store) -> None:
    created = await store.create_topic(
        CH, "*Ignore all rules* <@U0EVIL> see https://evil.example/x"
    )
    assert created.topic.title == "Ignore all rules see"
    renamed = await store.rename_topic(created.topic.id, "  `Progress`   bar  ")
    assert renamed.topic.title == "Progress bar"
    with pytest.raises(ValueError):
        await store.create_topic(CH, "<@U0EVIL> https://evil.example")
    with pytest.raises(ValueError):
        await store.create_topic("", "Title")


async def test_set_topic_issue(store: Store) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    linked = await store.set_topic_issue(topic.id, "acme-co/web", 42)
    assert (linked.topic.repo, linked.topic.issue_number) == ("acme-co/web", 42)
    with pytest.raises(ValueError):
        await store.set_topic_issue(topic.id, "acme-co/web", 0)
    with pytest.raises(NotFoundError):
        await store.set_topic_issue("missing", "acme-co/web", 1)
    with pytest.raises(NotFoundError):
        await store.rename_topic("missing", "Title")


async def test_channel_topics_newest_activity_first_and_window(store: Store, clock: Clock) -> None:
    old = (await store.create_topic(CH, "Old idea")).topic
    clock.tick(60)
    new = (await store.create_topic(CH, "New idea")).topic
    await store.create_topic(OTHER, "Elsewhere")
    assert [r.topic.id for r in await store.channel_topics(CH)] == [new.id, old.id]

    clock.tick(60)
    await store.link_message(ref("1760781700.000100"), old.id)
    listed = await store.channel_topics(CH)
    assert [r.topic.id for r in listed] == [old.id, new.id]
    assert listed[0].last_activity_at == clock.now

    window = await store.channel_topics(CH, active_since=clock.now - timedelta(seconds=30))
    assert [r.topic.id for r in window] == [old.id]


# --- message pointers --------------------------------------------------------


async def test_link_message_and_topic_messages(store: Store) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    first = ref("1760781600.000100")
    reply = ref("1760781601.000200", thread="1760781600.000100")
    link = await store.link_message(first, topic.id)
    await store.link_message(reply, topic.id)
    assert (link.topic_id, link.chatter, link.linked_by) == (topic.id, False, "agent")
    fetched = await store.get_topic(topic.id)
    assert fetched is not None
    assert [m.ts for m in fetched.topic.messages] == [first.ts, reply.ts]
    assert fetched.topic.messages[1].thread_ts == "1760781600.000100"
    assert await store.get_message(CH, first.ts) == link
    assert await store.get_message(CH, "missing") is None


async def test_link_message_rules(store: Store) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    with pytest.raises(NotFoundError):
        await store.link_message(ref("1.1"), "missing")
    with pytest.raises(ConflictError):
        await store.link_message(ref("1.1", channel=OTHER), topic.id)
    with pytest.raises(ValueError):
        await store.link_message(ref(""), topic.id)


async def test_a_person_decision_beats_the_agent(store: Store) -> None:
    a = (await store.create_topic(CH, "Topic A")).topic
    b = (await store.create_topic(CH, "Topic B")).topic
    message = ref("1760781600.000100")
    await store.link_message(message, a.id)
    moved = await store.link_message(message, b.id, by="person")
    assert (moved.topic_id, moved.linked_by) == (b.id, "person")

    kept = await store.link_message(message, a.id, by="agent")
    assert (kept.topic_id, kept.linked_by) == (b.id, "person")
    kept = await store.mark_chatter(message, by="agent")
    assert kept.topic_id == b.id

    chatter = await store.mark_chatter(message, by="person")
    assert (chatter.topic_id, chatter.chatter) == (None, True)
    topic_b = await store.get_topic(b.id)
    assert topic_b is not None
    assert topic_b.topic.messages == []


async def test_agent_can_revise_its_own_decision(store: Store) -> None:
    topic = (await store.create_topic(CH, "Topic")).topic
    message = ref("1760781600.000100")
    assert (await store.mark_chatter(message)).chatter
    relinked = await store.link_message(message, topic.id)
    assert (relinked.topic_id, relinked.chatter) == (topic.id, False)


# --- proposals ---------------------------------------------------------------


async def test_add_and_get_proposal(store: Store) -> None:
    stored = await store.add_proposal(record(participants=["U0BOB", "U0ALICE", "U0BOB"]))
    assert stored.state is S.PENDING
    assert stored.participants == ["U0ALICE", "U0BOB"]
    assert stored.proposal == draft()
    assert await store.get_proposal("p1") == stored
    assert await store.get_proposal("missing") is None


async def test_add_proposal_rules(store: Store) -> None:
    await store.add_proposal(record())
    with pytest.raises(ConflictError):
        await store.add_proposal(record())
    with pytest.raises(ValueError):
        await store.add_proposal(record("p2").model_copy(update={"state": S.APPROVED}))
    with pytest.raises(ValueError):
        await store.add_proposal(record("p3").model_copy(update={"approved_by": "U0X"}))
    with pytest.raises(NotFoundError):
        await store.add_proposal(record("p4", proposal=draft(topic_id="missing")))
    other = (await store.create_topic(OTHER, "Elsewhere")).topic
    with pytest.raises(ConflictError):
        await store.add_proposal(record("p5", proposal=draft(topic_id=other.id)))


async def test_update_proposals_round_trip(store: Store) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    update = UpdateProposal(
        topic_id=topic.id,
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(comment="Also on mobile", add_labels=["mobile"]),
        confidence=0.8,
    )
    stored = await store.add_proposal(record(proposal=update))
    assert stored.proposal == update


async def test_thread_proposals_oldest_first(store: Store) -> None:
    t0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    await store.add_proposal(record("b", created=t0 + timedelta(seconds=5)))
    await store.add_proposal(record("a", created=t0))
    await store.add_proposal(record("c", thread="1760781700.000200"))
    await store.add_proposal(record("d", channel=OTHER))
    assert [p.id for p in await store.thread_proposals(CH, "1760781600.000100")] == ["a", "b"]
    assert [p.id for p in await store.thread_proposals(CH, "1760781700.000200")] == ["c"]


@pytest.mark.parametrize("thread", [None, ""])
async def test_a_proposal_needs_its_thread(store: Store, thread: str | None) -> None:
    # A top-level post is its own thread, so a proposal always names one.
    with pytest.raises(ValueError):
        await store.add_proposal(record("p1", thread=thread))


async def test_approval_locks_the_content_hash(store: Store, clock: Clock) -> None:
    await store.add_proposal(record())
    shown = content_hash(draft())
    clock.tick(30)
    done = await store.transition("p1", S.APPROVED, approved_by="U0CAROL", expected_hash=shown)
    assert done.state is S.APPROVED
    assert done.content_hash == shown
    assert done.approved_by == "U0CAROL"
    assert done.approved_at == clock.now


async def test_stale_card_hash_refuses_approval(store: Store) -> None:
    await store.add_proposal(record())
    stale = content_hash(draft(title="Something else"))
    with pytest.raises(ContentChangedError):
        await store.transition("p1", S.APPROVED, approved_by="U0CAROL", expected_hash=stale)
    still = await store.get_proposal("p1")
    assert still is not None
    assert (still.state, still.content_hash, still.approved_by) == (S.PENDING, None, None)


async def test_auto_mode_approval(store: Store) -> None:
    await store.add_proposal(record())
    done = await store.transition("p1", S.APPROVED, approved_by=AUTO_APPROVER)
    assert done.approved_by == "auto"


async def test_approving_needs_an_approver(store: Store) -> None:
    await store.add_proposal(record())
    with pytest.raises(ValueError):
        await store.transition("p1", S.APPROVED)


async def test_a_person_must_approve_what_the_card_showed(store: Store) -> None:
    await store.add_proposal(record())
    with pytest.raises(ValueError, match="expected_hash"):
        await store.transition("p1", S.APPROVED, approved_by="U0CAROL")
    still = await store.get_proposal("p1")
    assert still is not None and still.state is S.PENDING


async def test_full_happy_path(store: Store) -> None:
    await approved(store)
    await store.transition("p1", S.EXECUTING)
    result = ExecutionResult(issue_url=ISSUE_URL)
    applied = await store.transition("p1", S.APPLIED, result=result)
    assert applied.state is S.APPLIED
    assert applied.result == result
    assert applied.approved_by == "U0CAROL"


async def test_applying_needs_a_matching_url(store: Store) -> None:
    await approved(store)
    await store.transition("p1", S.EXECUTING)
    with pytest.raises(ValueError):
        await store.transition("p1", S.APPLIED)
    wrong = ExecutionResult(issue_url="https://github.com/acme-co/api/issues/42")
    with pytest.raises(ValueError):
        await store.transition("p1", S.APPLIED, result=wrong)
    still = await store.get_proposal("p1")
    assert still is not None
    assert still.state is S.EXECUTING


async def test_reconcile_and_retry(store: Store) -> None:
    first = await approved(store)
    await store.transition("p1", S.EXECUTING)
    await store.transition("p1", S.RECONCILING)
    retried = await store.transition("p1", S.APPROVED)
    assert retried.content_hash == first.content_hash
    assert retried.approved_by == first.approved_by
    assert retried.approved_at == first.approved_at
    await store.transition("p1", S.EXECUTING)
    failed = await store.transition("p1", S.FAILED, result=ExecutionResult(error="GitHub said no"))
    assert failed.result is not None and failed.result.error == "GitHub said no"
    again = await store.transition("p1", S.APPROVED, approved_by="U0DAVE", expected_hash=SHOWN)
    assert again.approved_by == "U0DAVE"


async def test_failed_result_must_be_an_error(store: Store) -> None:
    await approved(store)
    await store.transition("p1", S.EXECUTING)
    with pytest.raises(ValueError):
        await store.transition("p1", S.FAILED, result=ExecutionResult(issue_url=ISSUE_URL))
    failed = await store.transition("p1", S.FAILED)
    assert failed.result is None


@pytest.mark.parametrize("start", list(S))
@pytest.mark.parametrize("target", list(S))
async def test_only_diagram_edges_are_allowed(
    store: Store, start: ProposalState, target: ProposalState
) -> None:
    await _reach(store, start)
    kwargs: dict[str, object] = {}
    if target is S.APPROVED:
        kwargs["approved_by"] = "U0CAROL"
        kwargs["expected_hash"] = SHOWN
    if target is S.APPLIED:
        kwargs["result"] = ExecutionResult(issue_url=ISSUE_URL)
    if target in ALLOWED_TRANSITIONS[start]:
        assert (await store.transition("p1", target, **kwargs)).state is target  # type: ignore[arg-type]
    else:
        with pytest.raises(TransitionError) as error:
            await store.transition("p1", target, **kwargs)  # type: ignore[arg-type]
        assert error.value.current is start
        still = await store.get_proposal("p1")
        assert still is not None and still.state is start


async def _reach(store: Store, state: ProposalState) -> None:
    await store.add_proposal(record())
    paths: dict[ProposalState, list[ProposalState]] = {
        S.PENDING: [],
        S.APPROVED: [S.APPROVED],
        S.EXECUTING: [S.APPROVED, S.EXECUTING],
        S.RECONCILING: [S.APPROVED, S.EXECUTING, S.RECONCILING],
        S.APPLIED: [S.APPROVED, S.EXECUTING, S.APPLIED],
        S.FAILED: [S.APPROVED, S.EXECUTING, S.FAILED],
        S.REJECTED: [S.REJECTED],
        S.SUPERSEDED: [S.SUPERSEDED],
    }
    for step in paths[state]:
        kwargs: dict[str, object] = {}
        if step is S.APPROVED:
            kwargs["approved_by"] = "U0CAROL"
            kwargs["expected_hash"] = SHOWN
        if step is S.APPLIED:
            kwargs["result"] = ExecutionResult(issue_url=ISSUE_URL)
        await store.transition("p1", step, **kwargs)  # type: ignore[arg-type]


async def test_expected_states_guard(store: Store) -> None:
    await approved(store)
    with pytest.raises(TransitionError):
        await store.transition("p1", S.EXECUTING, expected=[S.RECONCILING])
    assert (await store.transition("p1", S.EXECUTING, expected=[S.APPROVED])).state is S.EXECUTING


async def test_misplaced_arguments_are_refused(store: Store) -> None:
    await store.add_proposal(record())
    with pytest.raises(ValueError):
        await store.transition("p1", S.REJECTED, approved_by="U0CAROL", expected_hash=SHOWN)
    with pytest.raises(ValueError):
        await store.transition("p1", S.REJECTED, result=ExecutionResult(error="x"))
    with pytest.raises(NotFoundError):
        await store.transition("missing", S.REJECTED)


async def test_concurrent_approvals_only_one_wins(store: Store) -> None:
    await store.add_proposal(record())
    await store.transition("p1", S.APPROVED, approved_by="U0CAROL", expected_hash=SHOWN)
    outcomes = await asyncio.gather(
        *(store.transition("p1", S.EXECUTING) for _ in range(10)), return_exceptions=True
    )
    wins = [o for o in outcomes if isinstance(o, ProposalRecord)]
    losses = [o for o in outcomes if isinstance(o, TransitionError)]
    assert len(wins) == 1 and len(losses) == 9


async def test_supersede_pending(store: Store) -> None:
    for proposal_id in ("old1", "old2", "new"):
        await store.add_proposal(record(proposal_id))
    await store.add_proposal(record("elsewhere", thread="1760781700.000100"))
    await store.add_proposal(record("done"))
    await store.transition("done", S.APPROVED, approved_by="U0CAROL", expected_hash=SHOWN)
    assert await store.supersede_pending(CH, "1760781600.000100", keep_id="new") == ["old1", "old2"]
    states = {p.id: p.state for p in await store.thread_proposals(CH, "1760781600.000100")}
    assert states == {
        "old1": S.SUPERSEDED,
        "old2": S.SUPERSEDED,
        "new": S.PENDING,
        "done": S.APPROVED,
    }
    elsewhere = await store.get_proposal("elsewhere")
    assert elsewhere is not None and elsewhere.state is S.PENDING
    assert await store.supersede_pending(CH, "1760781600.000100", keep_id="new") == []


# --- identity links and channel modes ----------------------------------------


async def test_identity_links(store: Store, clock: Clock) -> None:
    link = await store.link_identity("U0ALICE", "alice-dev")
    assert (link.github_login, link.verified, link.linked_at) == ("alice-dev", False, clock.now)
    assert await store.get_identity("U0ALICE") == link
    relinked = await store.link_identity("U0ALICE", "alice-new")
    assert relinked.github_login == "alice-new"
    with pytest.raises(ConflictError):
        await store.link_identity("U0BOB", "ALICE-NEW")
    with pytest.raises(ValueError):
        await store.link_identity("U0BOB", "-not-a-login")
    assert await store.unlink_identity("U0ALICE") is True
    assert await store.unlink_identity("U0ALICE") is False
    assert await store.get_identity("U0ALICE") is None
    assert (await store.link_identity("U0BOB", "alice-new")).slack_user_id == "U0BOB"


async def test_channel_modes(store: Store, clock: Clock) -> None:
    assert await store.get_channel_mode(CH) is None
    setting = await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by="U0ALICE")
    assert (setting.mode, setting.changed_by, setting.changed_at) == (
        ChannelMode.AUTO,
        "U0ALICE",
        clock.now,
    )
    clock.tick()
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0BOB")
    current = await store.get_channel_mode(CH)
    assert current is not None and (current.mode, current.changed_by) == (
        ChannelMode.PAUSED,
        "U0BOB",
    )
    with pytest.raises(ValueError):
        await store.set_channel_mode(CH, "loud", changed_by="U0BOB")  # type: ignore[arg-type]


async def test_naive_times_are_refused(store: Store, clock: Clock) -> None:
    clock.now = datetime(2026, 10, 18, 9, 0)  # no timezone
    with pytest.raises(ValueError):
        await store.create_topic(CH, "Title")


async def test_two_connections_to_one_file_never_both_win(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "shared.db"
    first = await SqliteStore.open(path, clock=clock)
    second = await SqliteStore.open(path, clock=clock)
    try:
        await first.add_proposal(record())
        await first.transition("p1", S.APPROVED, approved_by="U0CAROL", expected_hash=SHOWN)
        outcomes = await asyncio.gather(
            *(s.transition("p1", S.EXECUTING) for s in (first, second, first, second)),
            return_exceptions=True,
        )
        assert sum(isinstance(o, ProposalRecord) for o in outcomes) == 1
        assert sum(isinstance(o, TransitionError) for o in outcomes) == 3
    finally:
        await first.close()
        await second.close()


async def test_try_transition_reports_a_lost_race(store: Store) -> None:
    await approved(store)
    outcomes = await asyncio.gather(*(try_transition(store, "p1", S.EXECUTING) for _ in range(5)))
    assert sum(o is not None for o in outcomes) == 1
    assert await try_transition(store, "p1", S.REJECTED) is None
    with pytest.raises(NotFoundError):
        await try_transition(store, "missing", S.REJECTED)


# --- #102: drafts are cleared once a proposal is final -------------------------


def _assert_cleared(proposal: ProposalRecord, clock: Clock) -> None:
    assert proposal.draft_cleared_at == clock.now
    draft_now = proposal.proposal
    assert isinstance(draft_now, IssueProposal)
    assert (draft_now.title, draft_now.body) == (CLEARED_TITLE, "")
    kept = draft()
    assert (draft_now.repo, draft_now.labels, draft_now.assignees) == (
        kept.repo,
        kept.labels,
        kept.assignees,
    )


async def test_applied_clears_the_draft_and_keeps_the_audit_trail(
    store: Store, clock: Clock
) -> None:
    first = await approved(store)
    await store.transition("p1", S.EXECUTING)
    clock.tick(5)
    applied = await store.transition("p1", S.APPLIED, result=ExecutionResult(issue_url=ISSUE_URL))
    _assert_cleared(applied, clock)
    assert applied.content_hash == first.content_hash == SHOWN
    assert (applied.approved_by, applied.approved_at) == (first.approved_by, first.approved_at)
    assert applied.result is not None and applied.result.issue_url == ISSUE_URL
    assert applied.participants == first.participants
    assert await store.get_proposal("p1") == applied


async def test_rejected_clears_the_draft(store: Store, clock: Clock) -> None:
    await store.add_proposal(record())
    rejected = await store.transition("p1", S.REJECTED)
    _assert_cleared(rejected, clock)
    assert rejected.content_hash is None


async def test_superseded_drafts_are_cleared(store: Store, clock: Clock) -> None:
    await store.add_proposal(record("old"))
    await store.add_proposal(record("new"))
    clock.tick()
    assert await store.supersede_pending(CH, "1760781600.000100", keep_id="new") == ["old"]
    old = await store.get_proposal("old")
    assert old is not None
    _assert_cleared(old, clock)
    new = await store.get_proposal("new")
    assert new is not None and new.draft_cleared_at is None and new.proposal == draft()


async def test_update_drafts_lose_body_and_comment_only(store: Store, clock: Clock) -> None:
    topic = (await store.create_topic(CH, "Progress bar")).topic
    update = UpdateProposal(
        topic_id=topic.id,
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(
            body="New body", comment="Also on mobile", add_labels=["mobile"], add_assignees=["a"]
        ),
        sources=[ref("1760781600.000100")],
        confidence=0.8,
    )
    await store.add_proposal(record(proposal=update))
    rejected = await store.transition("p1", S.REJECTED)
    cleared = rejected.proposal
    assert isinstance(cleared, UpdateProposal)
    assert (cleared.changes.body, cleared.changes.comment) == (None, None)
    assert cleared.changes.add_labels == ["mobile"] and cleared.changes.add_assignees == ["a"]
    assert (cleared.issue_number, cleared.topic_id) == (42, topic.id)
    assert [s.ts for s in cleared.sources] == ["1760781600.000100"]


async def test_a_failed_draft_stays_for_retry_until_cleared(store: Store, clock: Clock) -> None:
    await approved(store)
    await store.transition("p1", S.EXECUTING)
    failed = await store.transition("p1", S.FAILED, result=ExecutionResult(error="GitHub said no"))
    assert failed.draft_cleared_at is None and failed.proposal == draft()

    clock.tick()
    cleared = await store.clear_draft("p1")
    _assert_cleared(cleared, clock)
    assert cleared.state is S.FAILED
    assert cleared.result is not None and cleared.result.error == "GitHub said no"
    with pytest.raises(DraftClearedError):
        await store.transition("p1", S.APPROVED, approved_by="U0DAVE", expected_hash=SHOWN)
    assert (
        await try_transition(store, "p1", S.APPROVED, approved_by="U0DAVE", expected_hash=SHOWN)
        is None
    )
    again = await store.clear_draft("p1")
    assert again.draft_cleared_at == cleared.draft_cleared_at


@pytest.mark.parametrize("state", [S.PENDING, S.APPROVED, S.EXECUTING, S.RECONCILING])
async def test_a_live_draft_cannot_be_cleared(store: Store, state: ProposalState) -> None:
    await _reach(store, state)
    with pytest.raises(TransitionError, match="so its draft is still needed"):
        await store.clear_draft("p1")
    still = await store.get_proposal("p1")
    assert still is not None and still.draft_cleared_at is None and still.proposal == draft()


async def test_clearing_a_missing_proposal(store: Store) -> None:
    with pytest.raises(NotFoundError):
        await store.clear_draft("missing")


@pytest.mark.parametrize("final", sorted(FINAL_STATES))
async def test_clear_draft_is_a_no_op_once_final(
    store: Store, clock: Clock, final: ProposalState
) -> None:
    await _reach(store, final)
    first = await store.get_proposal("p1")
    clock.tick(60)
    assert await store.clear_draft("p1") == first


@pytest.mark.parametrize("path", ["rejected", "applied", "superseded", "cleared after failing"])
async def test_cleared_text_is_gone_from_the_files_while_open(
    tmp_path: Path, clock: Clock, path: str
) -> None:
    """secure_delete scrubs the main file and a checkpoint resets the WAL, so while
    the store is still open no file holds the cleared text."""
    secret = "SECRET-THREAD-QUOTE " * 50
    sqlite_store = await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)

    def leftovers() -> list[str]:
        return [p.name for p in tmp_path.iterdir() if b"SECRET-THREAD-QUOTE" in p.read_bytes()]

    try:
        await sqlite_store.add_proposal(record(proposal=draft(body=secret)))
        assert leftovers()  # the draft is really on disk before the clear
        if path == "rejected":
            await sqlite_store.transition("p1", S.REJECTED)
        elif path == "superseded":
            await sqlite_store.add_proposal(record("p2"))
            await sqlite_store.supersede_pending(CH, "1760781600.000100", keep_id="p2")
        else:
            hashed = content_hash(draft(body=secret))
            await sqlite_store.transition(
                "p1", S.APPROVED, approved_by="U0CAROL", expected_hash=hashed
            )
            await sqlite_store.transition("p1", S.EXECUTING)
            if path == "applied":
                await sqlite_store.transition(
                    "p1", S.APPLIED, result=ExecutionResult(issue_url=ISSUE_URL)
                )
            else:
                await sqlite_store.transition("p1", S.FAILED)
                assert leftovers()  # a failed draft is kept for a retry
                await sqlite_store.clear_draft("p1")
        assert leftovers() == []
    finally:
        await sqlite_store.close()
    assert leftovers() == []


async def test_an_in_memory_database_has_no_log_to_scrub(clock: Clock) -> None:
    memory_db = await SqliteStore.open(":memory:", clock=clock)
    async with memory_db:
        await memory_db.add_proposal(record())
        rejected = await memory_db.transition("p1", S.REJECTED)
    assert rejected.draft_cleared_at == clock.now


# Write-tool ledger and packet membership (#15)


def call(clock: Clock, **overrides: object) -> ToolCallRecord:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "tool": "propose_issue",
        "args_hash": "sha256:aa",
        "channel_id": CH,
        "thread_ts": "1.000100",
        "result_json": '{"proposal_id": "p1"}',
        "called_at": clock(),
    }
    fields.update(overrides)
    return ToolCallRecord(**fields)  # type: ignore[arg-type]


async def test_tool_calls_are_numbered_per_run_and_found_by_hash(
    store: Store, clock: Clock
) -> None:
    assert await store.find_tool_call("run-1", "propose_issue", "sha256:aa") is None
    assert await store.record_tool_call(call(clock)) == 1
    assert await store.record_tool_call(call(clock, args_hash="sha256:bb")) == 2
    assert await store.record_tool_call(call(clock, run_id="run-2")) == 1
    assert await store.find_tool_call("run-1", "propose_issue", "sha256:aa") == (
        '{"proposal_id": "p1"}'
    )
    assert await store.find_tool_call("run-1", "ask_channel", "sha256:aa") is None


async def test_the_same_call_cannot_be_recorded_twice(store: Store, clock: Clock) -> None:
    await store.record_tool_call(call(clock))
    with pytest.raises(ConflictError, match="already recorded"):
        await store.record_tool_call(call(clock, result_json="{}"))


async def test_tool_calls_are_counted_per_run_and_per_thread(store: Store, clock: Clock) -> None:
    start = clock()
    await store.record_tool_call(call(clock))
    await store.record_tool_call(call(clock, tool="ask_channel", args_hash="sha256:q1"))
    clock.tick(60)
    await store.record_tool_call(
        call(clock, run_id="run-2", tool="ask_channel", args_hash="sha256:q2")
    )
    assert await store.count_tool_calls("run-1", ["propose_issue", "propose_update"]) == 1
    assert await store.count_tool_calls("run-1", ["ask_channel"]) == 1
    assert await store.count_tool_calls("run-1", []) == 0
    assert await store.count_thread_calls(CH, "1.000100", "ask_channel", since=start) == 2
    assert (
        await store.count_thread_calls(
            CH, "1.000100", "ask_channel", since=start + timedelta(seconds=30)
        )
        == 1
    )
    assert await store.count_thread_calls(CH, "9.000100", "ask_channel", since=start) == 0


async def test_packet_membership_is_per_run(store: Store) -> None:
    await store.record_packet("run-1", [ref("1.000100"), ref("1.000200", thread="1.000100")])
    await store.record_packet("run-1", [ref("1.000100")])  # recording again is harmless
    assert await store.packet_ref("run-1", CH, "1.000200") == ref("1.000200", thread="1.000100")
    assert await store.packet_ref("run-1", CH, "1.000100") == ref("1.000100")
    assert await store.packet_ref("run-2", CH, "1.000200") is None
    assert await store.packet_ref("run-1", OTHER, "1.000200") is None
    assert await store.packet_ref("run-1", CH, "9.000100") is None
