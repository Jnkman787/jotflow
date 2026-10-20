"""Run records (#53): the same contract for MemoryStore and SqliteStore."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from jotflow.models import ProposalState, RunKind, RunStatus
from jotflow.store import (
    AUTO_APPROVER,
    DEFAULT_RUN_TIMEOUTS,
    NotFoundError,
    SqliteStore,
    Store,
)
from tests.test_store_contract import CH, OTHER, Clock, clock, record, store

__all__ = ["clock", "store"]  # pytest fixtures, imported for use here

T1 = "1760781600.000100"
T2 = "1760781700.000200"
T3 = "1760781800.000300"
D, E, Q = RunKind.DRAFTING, RunKind.EXECUTION, RunKind.QUESTION


async def approved(store: Store, proposal_id: str = "p1", thread: str = T1) -> None:
    await store.add_proposal(record(proposal_id, thread=thread))
    await store.transition(proposal_id, ProposalState.APPROVED, approved_by=AUTO_APPROVER)


# Enqueue and coalescing


async def test_enqueue_records_a_queued_run(store: Store, clock: Clock) -> None:
    run = await store.enqueue_run(D, CH, T1)
    assert run.status is RunStatus.QUEUED
    assert (run.kind, run.channel_id, run.thread_ts, run.proposal_id) == (D, CH, T1, None)
    assert run.timeout_seconds == DEFAULT_RUN_TIMEOUTS[D].total_seconds()
    assert run.created_at == clock.now
    assert run.deadline == clock.now + DEFAULT_RUN_TIMEOUTS[D]
    assert await store.get_run(run.id) == run


async def test_a_burst_coalesces_into_one_queued_drafting_run(store: Store) -> None:
    first = await store.enqueue_run(D, CH, T1)
    again = await store.enqueue_run(D, CH, T1)
    assert again.id == first.id
    assert len(await store.thread_runs(CH, T1)) == 1


async def test_each_top_level_post_is_its_own_thread(store: Store) -> None:
    # A top-level post's run carries the post's own ts, as Slack does for replies.
    first = await store.enqueue_run(D, CH, T1)
    second = await store.enqueue_run(D, CH, T2)
    assert second.id != first.id
    claimed = [await store.claim_next_run(max_running=4) for _ in range(2)]
    assert {c.id for c in claimed if c is not None} == {first.id, second.id}


async def test_a_reply_waits_for_its_posts_run(store: Store) -> None:
    # The post at T1 is being drafted; a reply to it has thread_ts T1 too.
    await store.enqueue_run(D, CH, T1)
    running = await store.claim_next_run(max_running=4)
    assert running is not None
    reply = await store.enqueue_run(D, CH, T1)
    assert await store.claim_next_run(max_running=4) is None
    await store.finish_run(running.id, RunStatus.SUCCEEDED)
    claimed = await store.claim_next_run(max_running=4)
    assert claimed is not None and claimed.id == reply.id


async def test_the_same_ts_in_another_channel_is_another_thread(store: Store) -> None:
    first = await store.enqueue_run(D, CH, T1)
    assert (await store.enqueue_run(D, OTHER, T1)).id != first.id


async def test_a_thread_ts_is_required(store: Store) -> None:
    with pytest.raises(ValueError, match="thread ts"):
        await store.enqueue_run(D, CH, "")


async def test_a_message_during_a_run_queues_one_follow_up(store: Store) -> None:
    first = await store.enqueue_run(D, CH, T1)
    claimed = await store.claim_next_run(max_running=4)
    assert claimed is not None and claimed.id == first.id
    follow_up = await store.enqueue_run(D, CH, T1)
    assert follow_up.id != first.id
    assert (await store.enqueue_run(D, CH, T1)).id == follow_up.id


async def test_an_execution_run_coalesces_per_proposal(store: Store) -> None:
    first = await store.enqueue_run(E, CH, T1, proposal_id="p1")
    assert (await store.enqueue_run(E, CH, T1, proposal_id="p1")).id == first.id
    assert (await store.enqueue_run(E, CH, T1, proposal_id="p2")).id != first.id
    await store.claim_next_run(max_running=4)
    assert (await store.enqueue_run(E, CH, T1, proposal_id="p1")).id == first.id


async def test_queued_question_runs_coalesce_per_thread(store: Store) -> None:
    # One queued question run per thread (#57): it answers every question asked
    # since JotFlow's last answer there, so a second asker isn't lost.
    first = await store.enqueue_run(Q, CH, T1)
    assert (await store.enqueue_run(Q, CH, T1)).id == first.id
    assert (await store.enqueue_run(Q, CH, T2)).id != first.id


async def test_custom_timeout(store: Store, clock: Clock) -> None:
    run = await store.enqueue_run(D, CH, T1, timeout=timedelta(seconds=90))
    assert run.timeout_seconds == 90
    assert run.deadline == clock.now + timedelta(seconds=90)


@pytest.mark.parametrize(
    ("kind", "proposal_id", "timeout", "fragment"),
    [
        (E, None, None, "needs proposal_id"),
        (D, "p1", None, "only an execution run"),
        (D, None, timedelta(0), "between 1 second and 2 hours"),
        (D, None, timedelta(hours=2, seconds=1), "between 1 second and 2 hours"),
    ],
)
async def test_bad_enqueue_requests(
    store: Store,
    kind: RunKind,
    proposal_id: str | None,
    timeout: timedelta | None,
    fragment: str,
) -> None:
    with pytest.raises(ValueError, match=fragment):
        await store.enqueue_run(kind, CH, T1, proposal_id=proposal_id, timeout=timeout)


# Claiming: one run per thread, and the cap


async def test_claim_starts_the_oldest_queued_run(store: Store, clock: Clock) -> None:
    first = await store.enqueue_run(D, CH, T1)
    clock.tick()
    await store.enqueue_run(D, CH, T2)
    clock.tick(5)
    claimed = await store.claim_next_run(max_running=4)
    assert claimed is not None and claimed.id == first.id
    assert claimed.status is RunStatus.RUNNING
    assert claimed.started_at == clock.now
    assert claimed.deadline == clock.now + DEFAULT_RUN_TIMEOUTS[D]


async def test_two_bursts_on_one_thread_never_run_in_parallel(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    assert await store.claim_next_run(max_running=4) is not None
    await store.enqueue_run(D, CH, T1)
    await store.enqueue_run(E, CH, T1, proposal_id="p1")
    # Both waiting runs are for the busy thread, so nothing else may start.
    assert await store.claim_next_run(max_running=4) is None


async def test_the_lock_covers_execution_runs(store: Store) -> None:
    await store.enqueue_run(E, CH, T1, proposal_id="p1")
    assert await store.claim_next_run(max_running=4) is not None
    await store.enqueue_run(D, CH, T1)
    assert await store.claim_next_run(max_running=4) is None


async def test_the_next_run_on_a_thread_starts_after_the_first_ends(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    first = await store.claim_next_run(max_running=4)
    assert first is not None
    follow_up = await store.enqueue_run(D, CH, T1)
    await store.finish_run(first.id, RunStatus.SUCCEEDED)
    claimed = await store.claim_next_run(max_running=4)
    assert claimed is not None and claimed.id == follow_up.id


async def test_other_threads_proceed_while_one_is_busy(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    await store.claim_next_run(max_running=4)
    await store.enqueue_run(D, CH, T1)
    other = await store.enqueue_run(D, CH, T2)
    claimed = await store.claim_next_run(max_running=4)
    assert claimed is not None and claimed.id == other.id


async def test_the_concurrency_cap(store: Store) -> None:
    for thread in (T1, T2, T3):
        await store.enqueue_run(D, CH, thread)
    assert await store.claim_next_run(max_running=2) is not None
    assert await store.claim_next_run(max_running=2) is not None
    assert await store.claim_next_run(max_running=2) is None
    assert await store.claim_next_run(max_running=3) is not None


async def test_runs_queued_in_the_same_instant_start_in_order(store: Store) -> None:
    queued = [await store.enqueue_run(D, CH, thread) for thread in (T1, T2, T3)]
    claimed = [await store.claim_next_run(max_running=4) for _ in queued]
    assert [c.id for c in claimed if c is not None] == [q.id for q in queued]


async def test_claim_needs_a_positive_cap(store: Store) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await store.claim_next_run(max_running=0)


async def test_concurrent_claims_never_double_start(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    claims = await asyncio.gather(*(store.claim_next_run(max_running=4) for _ in range(8)))
    assert len([c for c in claims if c is not None]) == 1


# Finishing


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT])
async def test_finish_a_running_run(store: Store, clock: Clock, status: RunStatus) -> None:
    await store.enqueue_run(D, CH, T1)
    run = await store.claim_next_run(max_running=1)
    assert run is not None
    clock.tick(3)
    error = None if status is RunStatus.SUCCEEDED else "the agent exited with status 1"
    finished = await store.finish_run(run.id, status, error=error)
    assert finished is not None
    assert (finished.status, finished.finished_at, finished.error) == (status, clock.now, error)


async def test_a_late_second_finish_loses(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    run = await store.claim_next_run(max_running=1)
    assert run is not None
    assert await store.finish_run(run.id, RunStatus.TIMED_OUT) is not None
    assert await store.finish_run(run.id, RunStatus.SUCCEEDED) is None
    stored = await store.get_run(run.id)
    assert stored is not None and stored.status is RunStatus.TIMED_OUT


async def test_a_queued_run_can_only_be_cancelled(store: Store) -> None:
    run = await store.enqueue_run(D, CH, T1)
    with pytest.raises(ValueError, match="cancelled"):
        await store.finish_run(run.id, RunStatus.SUCCEEDED)
    cancelled = await store.finish_run(run.id, RunStatus.FAILED, error="debounce reset")
    assert cancelled is not None and cancelled.started_at is None


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.INTERRUPTED])
async def test_finish_needs_a_finishing_status(store: Store, status: RunStatus) -> None:
    run = await store.enqueue_run(D, CH, T1)
    with pytest.raises(ValueError, match="not a finishing status"):
        await store.finish_run(run.id, status)


async def test_success_has_no_error(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    run = await store.claim_next_run(max_running=1)
    assert run is not None
    with pytest.raises(ValueError, match="no error"):
        await store.finish_run(run.id, RunStatus.SUCCEEDED, error="oops")


async def test_a_long_error_is_bounded(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    run = await store.claim_next_run(max_running=1)
    assert run is not None
    finished = await store.finish_run(run.id, RunStatus.FAILED, error="x" * 5000)
    assert finished is not None and finished.error == "x" * 2000


async def test_finishing_an_unknown_run(store: Store) -> None:
    with pytest.raises(NotFoundError):
        await store.finish_run("missing", RunStatus.FAILED)


async def test_overdue_runs(store: Store, clock: Clock) -> None:
    await store.enqueue_run(D, CH, T1, timeout=timedelta(seconds=10))
    await store.enqueue_run(D, CH, T2, timeout=timedelta(seconds=60))
    first = await store.claim_next_run(max_running=4)
    second = await store.claim_next_run(max_running=4)
    assert first is not None and second is not None
    assert await store.overdue_runs() == []
    clock.tick(11)
    assert [r.id for r in await store.overdue_runs()] == [first.id]
    clock.tick(60)
    assert [r.id for r in await store.overdue_runs()] == [first.id, second.id]


async def test_thread_runs_lists_one_thread_oldest_first(store: Store, clock: Clock) -> None:
    a = await store.enqueue_run(D, CH, T1)
    await store.claim_next_run(max_running=1)
    clock.tick()
    b = await store.enqueue_run(D, CH, T1)
    await store.enqueue_run(D, CH, T2)
    assert [r.id for r in await store.thread_runs(CH, T1)] == [a.id, b.id]
    assert await store.thread_runs(OTHER, T1) == []


# Restart recovery


async def test_recovery_requeues_interrupted_drafting_once_per_thread(store: Store) -> None:
    running = await store.enqueue_run(D, CH, T1)
    await store.claim_next_run(max_running=4)
    queued = await store.enqueue_run(D, CH, T1)
    other = await store.enqueue_run(D, CH, T2)
    recovery = await store.recover_runs()
    assert set(recovery.interrupted) == {running.id, queued.id, other.id}
    assert len(recovery.requeued) == 2
    for thread in (T1, T2):
        statuses = [r.status for r in await store.thread_runs(CH, thread)]
        assert statuses.count(RunStatus.QUEUED) == 1
        assert RunStatus.RUNNING not in statuses
    assert recovery.reconciling == ()


async def test_recovery_marks_an_interrupted_write_for_reconciliation(store: Store) -> None:
    await approved(store)
    run = await store.enqueue_run(E, CH, T1, proposal_id="p1")
    await store.claim_next_run(max_running=4)
    await store.transition("p1", ProposalState.EXECUTING)
    recovery = await store.recover_runs()
    assert recovery.interrupted == (run.id,)
    assert recovery.reconciling == ("p1",)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING
    [fresh] = [r for r in await store.thread_runs(CH, T1) if r.status is RunStatus.QUEUED]
    assert (fresh.kind, fresh.proposal_id) == (E, "p1")


async def test_recovery_retries_a_write_that_never_started(store: Store) -> None:
    await approved(store)
    await store.enqueue_run(E, CH, T1, proposal_id="p1")
    recovery = await store.recover_runs()
    assert recovery.reconciling == ()
    assert len(recovery.requeued) == 1
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.APPROVED


async def test_recovery_does_not_retry_a_settled_proposal(store: Store) -> None:
    await store.add_proposal(record("p1"))
    await store.transition("p1", ProposalState.REJECTED)
    await store.enqueue_run(E, CH, T1, proposal_id="p1")
    await store.enqueue_run(E, CH, T1, proposal_id="gone")
    recovery = await store.recover_runs()
    assert len(recovery.interrupted) == 2
    assert recovery.requeued == ()


async def test_recovery_leaves_finished_runs_alone(store: Store) -> None:
    await store.enqueue_run(D, CH, T1)
    run = await store.claim_next_run(max_running=1)
    assert run is not None
    await store.finish_run(run.id, RunStatus.SUCCEEDED)
    recovery = await store.recover_runs()
    assert recovery.interrupted == () and recovery.requeued == ()


async def test_recovery_does_not_requeue_questions(store: Store) -> None:
    await store.enqueue_run(Q, CH, T1)
    recovery = await store.recover_runs()
    assert len(recovery.interrupted) == 1
    assert recovery.requeued == ()


async def test_recovery_queues_an_approved_proposal_that_never_got_a_run(store: Store) -> None:
    # The process stopped between approving the proposal and queuing its run.
    await approved(store)
    recovery = await store.recover_runs()
    assert recovery.interrupted == ()
    [fresh] = [r for r in await store.thread_runs(CH, T1) if r.status is RunStatus.QUEUED]
    assert (fresh.kind, fresh.proposal_id) == (E, "p1")
    assert recovery.requeued == (fresh.id,)


async def test_recovery_queues_one_run_per_waiting_proposal(store: Store) -> None:
    await approved(store, "p1", T1)
    await approved(store, "p2", T2)
    await store.enqueue_run(E, CH, T1, proposal_id="p1")
    recovery = await store.recover_runs()
    queued = [
        r
        for thread in (T1, T2)
        for r in await store.thread_runs(CH, thread)
        if r.status is RunStatus.QUEUED
    ]
    assert sorted(r.proposal_id or "" for r in queued) == ["p1", "p2"]
    assert len(recovery.requeued) == 2


async def test_recovery_keeps_the_interrupted_runs_timeout(store: Store) -> None:
    await approved(store)
    await store.enqueue_run(E, CH, T1, proposal_id="p1", timeout=timedelta(seconds=45))
    await store.recover_runs()
    [fresh] = [r for r in await store.thread_runs(CH, T1) if r.status is RunStatus.QUEUED]
    assert fresh.timeout_seconds == 45


async def test_recovery_with_nothing_in_flight(store: Store) -> None:
    recovery = await store.recover_runs()
    assert recovery.interrupted == recovery.requeued == recovery.reconciling == ()


# SQLite only: survival across a restart, and the database's own guarantees


async def test_runs_survive_a_restart(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "jotflow.db"
    first = await SqliteStore.open(path, clock=clock)
    await first.enqueue_run(D, CH, T1)
    running = await first.claim_next_run(max_running=1)
    await first.close()
    assert running is not None

    second = await SqliteStore.open(path, clock=clock)
    try:
        kept = await second.get_run(running.id)
        assert kept == running
        recovery = await second.recover_runs()
        assert recovery.interrupted == (running.id,)
        queued = await second.claim_next_run(max_running=1)
        assert queued is not None and queued.id == recovery.requeued[0]
    finally:
        await second.close()


async def test_the_database_refuses_two_running_runs_on_one_thread(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "jotflow.db")
    await store.close()
    rows = [
        ("r1", "running", "1760781600.000100"),
        ("r2", "running", "1760781600.000100"),
    ]
    with sqlite3.connect(tmp_path / "jotflow.db") as db:
        insert = (
            "INSERT INTO runs (id, kind, status, channel_id, thread_ts, timeout_seconds,"
            " created_at, deadline, started_at) VALUES (?, 'drafting', ?, ?, ?, 60, ?, ?, ?)"
        )
        when = "2026-10-18T09:00:00.000000+00:00"
        db.execute(insert, (rows[0][0], rows[0][1], CH, rows[0][2], when, when, when))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (rows[1][0], rows[1][1], CH, rows[1][2], when, when, when))


async def test_the_database_refuses_an_execution_run_without_a_proposal(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "jotflow.db")
    await store.close()
    when = "2026-10-18T09:00:00.000000+00:00"
    with sqlite3.connect(tmp_path / "jotflow.db") as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO runs (id, kind, status, channel_id, timeout_seconds, created_at,"
            " deadline) VALUES ('r1', 'execution', 'queued', ?, 60, ?, ?)",
            (CH, when, when),
        )


def test_no_run_outlives_its_token() -> None:
    from jotflow.models import MAX_RUN_SECONDS
    from jotflow.store.base import MAX_RUN_TIMEOUT_SECONDS
    from jotflow.whiteboard.tokens import MAX_TOKEN_TTL_SECONDS

    # One constant, read by the store and the token registry alike.
    assert MAX_RUN_TIMEOUT_SECONDS == MAX_TOKEN_TTL_SECONDS == MAX_RUN_SECONDS
    for timeout in DEFAULT_RUN_TIMEOUTS.values():
        assert timeout.total_seconds() <= MAX_RUN_SECONDS


async def test_a_two_hour_timeout_is_allowed(store: Store) -> None:
    run = await store.enqueue_run(D, CH, T1, timeout=timedelta(hours=2))
    assert run.timeout_seconds == 7200


async def test_claim_can_be_limited_to_some_kinds(store: Store) -> None:
    await approved(store)
    execution = await store.enqueue_run(E, CH, T1, proposal_id="p1")
    drafting = await store.enqueue_run(D, CH, T2)
    claimed = await store.claim_next_run(max_running=4, kinds={D, Q})
    assert claimed is not None and claimed.id == drafting.id
    assert await store.claim_next_run(max_running=4, kinds={D, Q}) is None
    left = await store.get_run(execution.id)
    assert left is not None and left.status is RunStatus.QUEUED
    assert await store.claim_next_run(max_running=4, kinds=()) is None
    later = await store.claim_next_run(max_running=4, kinds={E})
    assert later is not None and later.id == execution.id


# Threads waiting out their quiet window (#130)


async def test_an_armed_thread_is_pending_until_queued(store: Store, clock: Clock) -> None:
    due = clock.now + timedelta(seconds=120)
    await store.arm_thread(CH, T2, due + timedelta(seconds=5))
    await store.arm_thread(CH, T1, due)
    await store.arm_thread(CH, T2, due + timedelta(seconds=60))  # a later message pushes back
    pending = await store.pending_threads()
    assert [(p.channel_id, p.thread_ts, p.due_at) for p in pending] == [
        (CH, T1, due),
        (CH, T2, due + timedelta(seconds=60)),
    ]
    run = await store.queue_pending(CH, T1)
    assert run is not None and (run.kind, run.thread_ts, run.status) == (D, T1, RunStatus.QUEUED)
    assert [p.thread_ts for p in await store.pending_threads()] == [T2]
    # A second flush of the same thread queues nothing.
    assert await store.queue_pending(CH, T1) is None
    assert len(await store.thread_runs(CH, T1)) == 1


async def test_queue_pending_coalesces_with_a_queued_drafting_run(
    store: Store, clock: Clock
) -> None:
    queued = await store.enqueue_run(D, CH, T1)
    await store.arm_thread(CH, T1, clock.now)
    again = await store.queue_pending(CH, T1)
    assert again is not None and again.id == queued.id
    assert await store.pending_threads() == []


async def test_drop_pending_forgets_without_queuing(store: Store, clock: Clock) -> None:
    await store.arm_thread(CH, T1, clock.now)
    await store.drop_pending(CH, T1)
    await store.drop_pending(CH, T1)
    assert await store.pending_threads() == []
    assert await store.thread_runs(CH, T1) == []


async def test_arm_thread_checks_its_input(store: Store, clock: Clock) -> None:
    with pytest.raises(ValueError):
        await store.arm_thread("", T1, clock.now)
    with pytest.raises(ValueError):
        await store.arm_thread(CH, T1, clock.now.replace(tzinfo=None))


async def test_pending_threads_survive_reopening(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "jotflow.db"
    first = await SqliteStore.open(path, clock=clock)
    await first.arm_thread(CH, T1, clock.now)
    await first.close()
    second = await SqliteStore.open(path, clock=clock)
    try:
        assert [(p.thread_ts, p.due_at) for p in await second.pending_threads()] == [
            (T1, clock.now)
        ]
    finally:
        await second.close()


async def test_queue_pending_skips_a_thread_a_person_skipped_while_it_waited(
    store: Store, clock: Clock
) -> None:
    from jotflow.models import MessageRef

    await store.arm_thread(CH, T1, clock.now)
    await store.mark_chatter(MessageRef(channel_id=CH, ts=T1), by="person")
    assert await store.queue_pending(CH, T1) is None
    assert await store.pending_threads() == [] and await store.thread_runs(CH, T1) == []
    # The agent's chatter call is not a person's skip.
    await store.arm_thread(CH, T2, clock.now)
    await store.mark_chatter(MessageRef(channel_id=CH, ts=T2), by="agent")
    assert await store.queue_pending(CH, T2) is not None


# --- paused channels (#28) -----------------------------------------------------------


async def test_a_paused_channel_starts_no_run_until_it_is_unpaused(store: Store) -> None:
    from jotflow.models import ChannelMode

    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
    paused = await store.enqueue_run(RunKind.DRAFTING, CH, "1760781600.000100")
    elsewhere = await store.enqueue_run(RunKind.DRAFTING, OTHER, "1760781600.000100")
    claimed = await store.claim_next_run(max_running=5)
    assert claimed is not None and claimed.id == elsewhere.id
    assert await store.claim_next_run(max_running=5) is None
    await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by="U0LEAD")
    unpaused = await store.claim_next_run(max_running=5)
    assert unpaused is not None and unpaused.id == paused.id


async def test_a_paused_channels_cards_wait_until_it_is_unpaused(store: Store) -> None:
    from jotflow.models import ChannelMode

    await store.add_proposal(record("p1"))
    await store.add_proposal(record("p2", channel=OTHER))
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
    assert [p.id for p in await store.cards_due()] == ["p2"]
    await store.set_channel_mode(CH, ChannelMode.PROPOSE, changed_by="U0LEAD")
    assert [p.id for p in await store.cards_due()] == ["p1", "p2"]


async def test_an_auto_write_under_way_is_not_due_for_a_card(store: Store) -> None:
    from jotflow.store.hashing import content_hash

    await store.add_proposal(record("p1"))
    added = await store.get_proposal("p1")
    assert added is not None
    await store.transition(
        "p1", ProposalState.APPROVED, approved_by="auto", expected_hash=content_hash(added.proposal)
    )
    assert await store.cards_due() == []
    await store.transition("p1", ProposalState.EXECUTING)
    assert await store.cards_due() == []
    await store.transition("p1", ProposalState.FAILED)
    assert [p.id for p in await store.cards_due()] == ["p1"]


# A drafting run's outcome (#24)


async def test_a_finished_drafting_run_records_how_many_proposals_it_made(store: Store) -> None:
    empty = await store.enqueue_run(D, CH, T1)
    claimed = await store.claim_next_run(max_running=4)
    assert claimed is not None and claimed.id == empty.id
    await store.finish_run(empty.id, RunStatus.SUCCEEDED)
    ended = await store.get_run(empty.id)
    assert ended is not None and ended.status is RunStatus.SUCCEEDED
    assert await store.run_proposals(empty.id) == []  # it drafted nothing
    busy = await store.enqueue_run(D, CH, T2)
    first = record("p1", thread=T2).model_copy(update={"run_id": busy.id})
    second = record("p2", thread=T2).model_copy(update={"run_id": busy.id})
    await store.add_proposal(first)
    await store.add_proposal(second)
    await store.add_proposal(record("p3", thread=T3))  # another run's
    assert [p.id for p in await store.run_proposals(busy.id)] == ["p1", "p2"]
