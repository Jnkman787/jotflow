from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jotflow.models import IssueProposal, ProposalRecord, ProposalState, RunKind, RunStatus
from jotflow.orchestrator.runs import (
    GRACE_SECONDS,
    MAX_EXECUTION_ATTEMPTS,
    RETRY_DELAYS,
    TOKEN_FAILURE,
    RunLifecycle,
    StartedRun,
    grant_for,
    token_ttl,
)
from jotflow.policy import Policy
from jotflow.store import MemoryStore, SqliteStore, Store, content_hash
from jotflow.store.base import MAX_RUN_TIMEOUT_SECONDS
from jotflow.whiteboard.tokens import MAX_TOKEN_TTL_SECONDS, TokenRegistry
from jotflow.whiteboard.write_tools import RecordResultArgs, WriteTools

CH = "C0IDEAS01"
THREAD = "1760781600.000100"
START = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


class Clock:
    """One time source for the store (datetime) and the registry (monotonic seconds)."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def mono(self) -> float:
        return (self.now - START).total_seconds()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory", "sqlite"])
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, clock: Clock
) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore(clock=clock)
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)
    )
    yield made
    await made.close()


@pytest.fixture
def registry(clock: Clock) -> TokenRegistry:
    return TokenRegistry(clock=clock.mono)


@pytest.fixture
def lifecycle(store: Store, registry: TokenRegistry, clock: Clock) -> RunLifecycle:
    return RunLifecycle(store, registry, clock=clock)


async def _started(store: Store, lifecycle: RunLifecycle, **kwargs: object) -> StartedRun:
    await store.enqueue_run(RunKind.DRAFTING, CH, THREAD, **kwargs)  # type: ignore[arg-type]
    started = await lifecycle.start_next(max_running=2)
    assert started is not None
    return started


async def test_starting_a_run_issues_exactly_one_token(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    started = await _started(store, lifecycle)
    assert started.run.status is RunStatus.RUNNING
    grant = registry.check(started.token)
    assert grant is not None
    assert (grant.run_id, grant.kind, grant.channel_id, grant.thread_ts) == (
        started.run.id,
        RunKind.DRAFTING,
        CH,
        THREAD,
    )
    assert registry.active() == 1


async def test_nothing_to_start(lifecycle: RunLifecycle, registry: TokenRegistry) -> None:
    assert await lifecycle.start_next(max_running=2) is None
    assert registry.active() == 0


async def test_finishing_revokes_the_token(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    started = await _started(store, lifecycle)
    ended = await lifecycle.finish(started.run.id, RunStatus.SUCCEEDED)
    assert ended is not None and ended.status is RunStatus.SUCCEEDED
    assert registry.check(started.token) is None
    assert registry.active() == 0
    assert await lifecycle.finish(started.run.id, RunStatus.FAILED) is None


async def test_a_failed_run_also_loses_its_token(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    started = await _started(store, lifecycle)
    ended = await lifecycle.finish(started.run.id, RunStatus.FAILED, error="agent crashed")
    assert ended is not None and ended.error == "agent crashed"
    assert registry.check(started.token) is None


async def test_timing_out_revokes_the_token(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle, clock: Clock
) -> None:
    started = await _started(store, lifecycle, timeout=timedelta(minutes=5))
    assert await lifecycle.expire_overdue() == []
    clock.advance(5 * 60 + 1)
    assert await lifecycle.expire_overdue() == [started.run.id]
    run = await store.get_run(started.run.id)
    assert run is not None and run.status is RunStatus.TIMED_OUT
    assert registry.check(started.token) is None
    assert await lifecycle.expire_overdue() == []


async def test_recovery_revokes_interrupted_runs(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    started = await _started(store, lifecycle)
    recovery = await lifecycle.recover()
    assert started.run.id in recovery.interrupted
    assert registry.check(started.token) is None
    assert len(recovery.requeued) == 1


async def test_recovery_with_nothing_running(lifecycle: RunLifecycle) -> None:
    recovery = await lifecycle.recover()
    assert recovery.interrupted == ()


async def test_token_lasts_until_the_deadline_plus_grace(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle, clock: Clock
) -> None:
    started = await _started(store, lifecycle, timeout=timedelta(minutes=10))
    assert started.run.deadline == START + timedelta(minutes=10)
    clock.advance(10 * 60 + GRACE_SECONDS - 1)
    assert registry.check(started.token) is not None
    clock.advance(1)
    assert registry.check(started.token) is None


def test_token_ttl_is_capped_and_never_zero(clock: Clock) -> None:
    from jotflow.models import Run

    def run(deadline: datetime) -> Run:
        return Run(
            id="r1",
            kind=RunKind.DRAFTING,
            channel_id=CH,
            thread_ts=THREAD,
            created_at=START,
            deadline=deadline,
            timeout_seconds=MAX_RUN_TIMEOUT_SECONDS,
        )

    longest = run(START + timedelta(seconds=MAX_RUN_TIMEOUT_SECONDS))
    assert token_ttl(longest, START) == MAX_TOKEN_TTL_SECONDS
    past = run(START - timedelta(minutes=5))
    assert token_ttl(past, START) == 1.0


def test_the_run_timeout_cap_is_the_token_cap() -> None:
    assert MAX_RUN_TIMEOUT_SECONDS == MAX_TOKEN_TTL_SECONDS == 2 * 60 * 60


async def test_a_run_longer_than_the_cap_is_refused(store: Store) -> None:
    with pytest.raises(ValueError, match="2 hours"):
        await store.enqueue_run(
            RunKind.DRAFTING, CH, THREAD, timeout=timedelta(seconds=MAX_TOKEN_TTL_SECONDS + 1)
        )


async def test_a_token_failure_fails_the_run(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    full = TokenRegistry(clock=clock.mono, max_live=0)
    lifecycle = RunLifecycle(store, full, clock=clock)
    await store.enqueue_run(RunKind.DRAFTING, CH, THREAD)
    with pytest.raises(Exception, match="live run tokens"):
        await lifecycle.start_next(max_running=2)
    (run,) = await store.thread_runs(CH, THREAD)
    assert (run.status, run.error) == (RunStatus.FAILED, TOKEN_FAILURE)


async def test_a_token_failure_on_an_execution_run_reconciles_its_proposal(
    store: Store, clock: Clock
) -> None:
    full = TokenRegistry(clock=clock.mono, max_live=0)
    lifecycle = RunLifecycle(store, full, clock=clock)
    await _approved(store)
    with pytest.raises(Exception, match="live run tokens"):
        await _start_execution(store, lifecycle)
    (run,) = await store.thread_runs(CH, THREAD)
    assert (run.status, run.error) == (RunStatus.FAILED, TOKEN_FAILURE)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING


async def test_the_token_is_never_stored_or_logged(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sqlite_store = await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)
    registry = TokenRegistry(clock=clock.mono)
    lifecycle = RunLifecycle(sqlite_store, registry, clock=clock)
    try:
        started = await _started(sqlite_store, lifecycle)
        await lifecycle.finish(started.run.id, RunStatus.SUCCEEDED)
        assert started.token not in repr(started)
        assert "jfr_" not in repr(started)
    finally:
        await sqlite_store.close()
    for file in tmp_path.iterdir():
        assert started.token.encode() not in file.read_bytes(), file.name
    assert started.token not in caplog.text
    assert started.run.id in caplog.text


async def test_a_run_ended_elsewhere_is_not_reported_as_timed_out(
    clock: Clock, registry: TokenRegistry
) -> None:
    class Racing(MemoryStore):
        """Lists a run as overdue that another path finishes first."""

        async def overdue_runs(self):  # type: ignore[no-untyped-def]
            runs = await super().overdue_runs()
            for run in runs:
                await self.finish_run(run.id, RunStatus.SUCCEEDED)
            return runs

    racing = Racing(clock=clock)
    lifecycle = RunLifecycle(racing, registry, clock=clock)
    started = await _started(racing, lifecycle, timeout=timedelta(minutes=1))
    clock.advance(61)
    assert await lifecycle.expire_overdue() == []
    assert registry.check(started.token) is None


# --- execution runs move their proposal to executing ---------------------------

ISSUE_URL = "https://github.com/acme-co/web/issues/42"


def _proposal_record(proposal_id: str = "p1") -> ProposalRecord:
    return ProposalRecord(
        id=proposal_id,
        channel_id=CH,
        thread_ts=THREAD,
        run_id="draft-run",
        proposal=IssueProposal(
            repo="acme-co/web", title="Progress bar", body="Add one.", confidence=0.9
        ),
        created_at=START,
    )


async def _approved(store: Store, proposal_id: str = "p1") -> None:
    record = await store.add_proposal(_proposal_record(proposal_id))
    await store.transition(
        proposal_id,
        ProposalState.APPROVED,
        approved_by="U0CAROL",
        expected_hash=content_hash(record.proposal),
    )


async def _start_execution(
    store: Store, lifecycle: RunLifecycle, proposal_id: str = "p1"
) -> StartedRun | None:
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id=proposal_id)
    return await lifecycle.start_next(max_running=2)


async def test_an_approved_proposal_becomes_executing(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    await _approved(store)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.EXECUTING
    grant = registry.check(started.token)
    assert grant is not None
    assert (grant.kind, grant.proposal_id) == (RunKind.EXECUTION, "p1")
    assert grant_for(started.run) == grant


async def test_record_result_succeeds_after_start(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle, clock: Clock
) -> None:
    """End to end: start_next, then the agent's record_result through the real tool."""
    await _approved(store)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    grant = registry.check(started.token)
    assert grant is not None
    tools = WriteTools(store, POLICY, _NoPoster(), clock=clock)
    outcome = await tools.record_result(grant, RecordResultArgs(issue_url=ISSUE_URL))
    assert outcome.model_dump()["state"] == ProposalState.APPLIED
    applied = await store.get_proposal("p1")
    assert applied is not None and applied.state is ProposalState.APPLIED


async def test_a_reconciling_proposal_stays_reconciling(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    await _approved(store)
    await store.transition("p1", ProposalState.EXECUTING)
    await store.transition("p1", ProposalState.RECONCILING)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING
    assert registry.check(started.token) is not None


@pytest.mark.parametrize("state", [ProposalState.PENDING, ProposalState.REJECTED])
async def test_any_other_state_fails_the_run_without_a_token(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle, state: ProposalState
) -> None:
    await store.add_proposal(_proposal_record())
    if state is ProposalState.REJECTED:
        await store.transition("p1", ProposalState.REJECTED)
    assert await _start_execution(store, lifecycle) is None
    assert registry.active() == 0
    (run,) = await store.thread_runs(CH, THREAD)
    assert run.status is RunStatus.FAILED
    assert run.error is not None and "not approved or reconciling" in run.error


async def test_a_missing_proposal_fails_the_run_without_a_token(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    assert await _start_execution(store, lifecycle, "missing") is None
    assert registry.active() == 0
    (run,) = await store.thread_runs(CH, THREAD)
    assert run.status is RunStatus.FAILED and "does not exist" in (run.error or "")


async def test_losing_the_move_to_executing_issues_no_token(
    clock: Clock, registry: TokenRegistry
) -> None:
    class Racing(MemoryStore):
        """Another run moves the proposal to executing first."""

        async def get_proposal(self, proposal_id):  # type: ignore[no-untyped-def]
            record = await super().get_proposal(proposal_id)
            if record is not None and record.state is ProposalState.APPROVED:
                await super().transition(proposal_id, ProposalState.EXECUTING)
            return record

    racing = Racing(clock=clock)
    lifecycle = RunLifecycle(racing, registry, clock=clock)
    await _approved(racing)
    assert await _start_execution(racing, lifecycle) is None
    assert registry.active() == 0
    (run,) = await racing.thread_runs(CH, THREAD)
    assert run.status is RunStatus.FAILED
    assert "stopped being approved" in (run.error or "")


class _NoPoster:
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        raise AssertionError("execution runs never post")


POLICY = Policy.model_validate(
    {
        "channels": {
            CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]}
        }
    }
)


async def test_a_stop_between_claim_and_move_is_recovered_once(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle
) -> None:
    """JotFlow stops after claiming an execution run but before moving its proposal."""
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    claimed = await store.claim_next_run(max_running=2)  # the claim, then the stop
    assert claimed is not None
    # Even had a token been issued before the stop, recovery must revoke it.
    token = registry.issue(grant_for(claimed), 600)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.APPROVED

    recovery = await lifecycle.recover()
    assert recovery.interrupted == (claimed.id,)
    assert registry.check(token) is None
    interrupted = await store.get_run(claimed.id)
    assert interrupted is not None and interrupted.status is RunStatus.INTERRUPTED
    still = await store.get_proposal("p1")
    assert still is not None and still.state is ProposalState.APPROVED
    queued = [
        r
        for r in await store.thread_runs(CH, THREAD)
        if r.status is RunStatus.QUEUED and r.proposal_id == "p1"
    ]
    assert len(queued) == 1 and list(recovery.requeued) == [queued[0].id]

    # The fresh run starts normally and moves the proposal to executing.
    started = await lifecycle.start_next(max_running=2)
    assert started is not None and started.run.id == queued[0].id
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.EXECUTING
    # A second stop, now mid-execution, again queues exactly one run for it.
    again = await lifecycle.recover()
    assert len(again.requeued) == 1


# --- an execution run that ends without a result leaves its proposal reconciling


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT])
async def test_an_execution_run_ending_without_a_result_reconciles(
    store: Store, lifecycle: RunLifecycle, status: RunStatus
) -> None:
    await _approved(store)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    assert await lifecycle.finish(started.run.id, status) is not None
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING


async def test_an_overdue_execution_run_reconciles(
    store: Store, lifecycle: RunLifecycle, clock: Clock
) -> None:
    await _approved(store)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    clock.advance(MAX_RUN_TIMEOUT_SECONDS + 1)
    assert await lifecycle.expire_overdue() == [started.run.id]
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING


async def test_a_recorded_result_is_kept_when_the_run_ends(
    store: Store, registry: TokenRegistry, lifecycle: RunLifecycle, clock: Clock
) -> None:
    await _approved(store)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    grant = registry.check(started.token)
    assert grant is not None
    tools = WriteTools(store, POLICY, _NoPoster(), clock=clock)
    await tools.record_result(grant, RecordResultArgs(issue_url=ISSUE_URL))
    await lifecycle.finish(started.run.id, RunStatus.FAILED, error="exited 1 after writing")
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.APPLIED


async def test_a_reconciling_run_that_fails_stays_reconciling(
    store: Store, lifecycle: RunLifecycle
) -> None:
    await _approved(store)
    await store.transition("p1", ProposalState.EXECUTING)
    await store.transition("p1", ProposalState.RECONCILING)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    await lifecycle.finish(started.run.id, RunStatus.FAILED)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING


async def test_a_drafting_run_ending_touches_no_proposal(
    store: Store, lifecycle: RunLifecycle
) -> None:
    await _approved(store)
    await store.transition("p1", ProposalState.EXECUTING)
    started = await _started(store, lifecycle)
    await lifecycle.finish(started.run.id, RunStatus.FAILED)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.EXECUTING


# --- retries under a limit (#30) ----------------------------------------------


class GaveUp:
    def __init__(self, fail: bool = False) -> None:
        self.records: list[ProposalRecord] = []
        self.fail = fail

    async def __call__(self, record: ProposalRecord) -> None:
        self.records.append(record)
        if self.fail:
            raise RuntimeError("slack is down")


def _retrying(store: Store, registry: TokenRegistry, clock: Clock, gave_up: GaveUp) -> RunLifecycle:
    return RunLifecycle(store, registry, clock=clock, on_gave_up=gave_up)


async def _fail_attempt(store: Store, lifecycle: RunLifecycle) -> None:
    started = await lifecycle.start_next(max_running=2)
    assert started is not None and started.run.kind is RunKind.EXECUTION
    await lifecycle.finish(started.run.id, RunStatus.FAILED, error="agent exited 1")


async def test_a_failed_attempt_is_retried_after_a_delay(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    gave_up = GaveUp()
    lifecycle = _retrying(store, registry, clock, gave_up)
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    await _fail_attempt(store, lifecycle)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.RECONCILING
    assert lifecycle.pending_retries() == {"p1": clock.now + RETRY_DELAYS[0]}
    assert await lifecycle.queue_due_retries() == []
    clock.advance(RETRY_DELAYS[0].total_seconds())
    (retry_id,) = await lifecycle.queue_due_retries()
    retry = await store.get_run(retry_id)
    assert retry is not None and retry.status is RunStatus.QUEUED and retry.proposal_id == "p1"
    assert lifecycle.pending_retries() == {} and gave_up.records == []


async def test_at_the_limit_the_proposal_fails_once_with_a_notice(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    gave_up = GaveUp()
    lifecycle = _retrying(store, registry, clock, gave_up)
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    for delay in (*RETRY_DELAYS, None):
        await _fail_attempt(store, lifecycle)
        if delay is not None:
            clock.advance(delay.total_seconds())
            assert len(await lifecycle.queue_due_retries()) == 1
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.FAILED
    assert [r.id for r in gave_up.records] == ["p1"]
    assert gave_up.records[0].state is ProposalState.FAILED
    executions = [r for r in await store.thread_runs(CH, THREAD) if r.kind is RunKind.EXECUTION]
    assert len(executions) == MAX_EXECUTION_ATTEMPTS
    assert lifecycle.pending_retries() == {}
    clock.advance(3600)
    assert await lifecycle.queue_due_retries() == []


async def test_a_recorded_result_is_never_retried(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    lifecycle = _retrying(store, registry, clock, GaveUp())
    await _approved(store)
    started = await _start_execution(store, lifecycle)
    assert started is not None
    grant = registry.check(started.token)
    assert grant is not None
    tools = WriteTools(store, POLICY, _NoPoster(), clock=clock)
    await tools.record_result(grant, RecordResultArgs(issue_url=ISSUE_URL))
    await lifecycle.finish(started.run.id, RunStatus.SUCCEEDED)
    assert lifecycle.pending_retries() == {}


async def test_the_limit_holds_across_a_restart(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    gave_up = GaveUp()
    lifecycle = _retrying(store, registry, clock, gave_up)
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    for delay in RETRY_DELAYS:
        await _fail_attempt(store, lifecycle)
        clock.advance(delay.total_seconds())
        await lifecycle.queue_due_retries()
    # The third attempt is running when JotFlow stops; recovery interrupts it
    # and queues another, which must not start: three attempts have ended.
    assert await lifecycle.start_next(max_running=2) is not None
    restarted = _retrying(store, TokenRegistry(clock=clock.mono), clock, gave_up)
    await restarted.recover()
    assert await restarted.start_next(max_running=2) is None
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.FAILED
    assert [r.id for r in gave_up.records] == ["p1"]


async def test_a_retry_and_recovery_leave_one_live_run(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    lifecycle = _retrying(store, registry, clock, GaveUp())
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    await _fail_attempt(store, lifecycle)
    await lifecycle.recover()  # queues one for the reconciling proposal
    clock.advance(RETRY_DELAYS[0].total_seconds())
    await lifecycle.queue_due_retries()  # coalesces with it
    live = [
        r
        for r in await store.thread_runs(CH, THREAD)
        if r.kind is RunKind.EXECUTION and r.status in (RunStatus.QUEUED, RunStatus.RUNNING)
    ]
    assert len(live) == 1


async def test_a_failing_notice_is_logged_not_raised(
    store: Store, registry: TokenRegistry, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle = RunLifecycle(
        store, registry, clock=clock, max_execution_attempts=1, on_gave_up=GaveUp(fail=True)
    )
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    with caplog.at_level(logging.ERROR, logger="jotflow.runs"):
        await _fail_attempt(store, lifecycle)
    assert "could not post the give-up notice" in caplog.text
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.FAILED


def test_the_attempt_limit_must_be_positive(store: Store, registry: TokenRegistry) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RunLifecycle(store, registry, max_execution_attempts=0)


async def test_a_retry_is_dropped_if_the_proposal_moved_on(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    lifecycle = RunLifecycle(store, registry, clock=clock)  # no notice wired
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    await _fail_attempt(store, lifecycle)
    await store.transition("p1", ProposalState.APPROVED)  # a person stepped in
    clock.advance(RETRY_DELAYS[0].total_seconds())
    assert await lifecycle.queue_due_retries() == []
    # Giving up on a proposal no longer reconciling changes nothing.
    record = await store.get_proposal("p1")
    assert record is not None
    await lifecycle._give_up(record)
    again = await store.get_proposal("p1")
    assert again is not None and again.state is ProposalState.APPROVED


async def test_giving_up_without_a_notice_still_fails_the_proposal(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    lifecycle = RunLifecycle(store, registry, clock=clock, max_execution_attempts=1)
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    await _fail_attempt(store, lifecycle)
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.FAILED


async def test_a_new_approval_starts_a_fresh_attempt_count(
    store: Store, registry: TokenRegistry, clock: Clock
) -> None:
    gave_up = GaveUp()
    lifecycle = _retrying(store, registry, clock, gave_up)
    await _approved(store)
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    for delay in (*RETRY_DELAYS, None):
        await _fail_attempt(store, lifecycle)
        if delay is not None:
            clock.advance(delay.total_seconds())
            await lifecycle.queue_due_retries()
    failed = await store.get_proposal("p1")
    assert failed is not None and failed.state is ProposalState.FAILED
    # A person approves it again from the card (#27).
    clock.advance(60)
    await store.transition(
        "p1",
        ProposalState.APPROVED,
        approved_by="U0CAROL",
        expected_hash=content_hash(failed.proposal),
    )
    await store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    await _fail_attempt(store, lifecycle)
    again = await store.get_proposal("p1")
    assert again is not None and again.state is ProposalState.RECONCILING
    assert "p1" in lifecycle.pending_retries()
    assert len(gave_up.records) == 1


async def test_a_drafting_runs_end_logs_how_many_proposals_it_made(
    store: Store, lifecycle: RunLifecycle, caplog: pytest.LogCaptureFixture
) -> None:
    started = await _started(store, lifecycle)
    with caplog.at_level(logging.INFO, logger="jotflow.runs"):
        await lifecycle.finish(started.run.id, RunStatus.SUCCEEDED)
    assert f"run {started.run.id} ended (succeeded) with 0 proposal(s)" in caplog.text
