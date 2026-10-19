"""Starting and ending agent runs, with their MCP tokens (#107).

Every run gets exactly one token while it may act, and loses it the moment it
stops. :class:`RunLifecycle` is the one place that starts and ends runs, so
that rule holds on every path: a normal finish, a timeout, and recovery at
startup.

A token lasts until the run's deadline plus :data:`GRACE_SECONDS`, never more
than :data:`~jotflow.whiteboard.tokens.MAX_TOKEN_TTL_SECONDS`. Run timeouts are
capped at the same constant (``MAX_RUN_TIMEOUT_SECONDS`` in the store), so no
run outlives its token; a run at the full two hours gets no grace.

An execution run's proposal moves from approved to executing as the run
starts, which is the state the agent's ``record_result`` requires. If the run
ends with the proposal still executing, it moves on to reconciling.

Tokens are never stored or logged. The registry keeps only digests,
:class:`StartedRun` hides the token from ``repr``, and this module logs run ids
only.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jotflow.models import ProposalRecord, ProposalState, Run, RunKind, RunStatus
from jotflow.store.base import (
    ACTIVE_RUN_STATUSES,
    Clock,
    Recovery,
    Store,
    try_transition,
    utc_now,
)
from jotflow.whiteboard.tokens import MAX_TOKEN_TTL_SECONDS, Grant, TokenRegistry

log = logging.getLogger("jotflow.runs")

# Lets an agent's last tool call land just after its deadline, while the run
# is being timed out.
GRACE_SECONDS = 60
# Execution runs per proposal, counting every ended one, before it fails (#30).
MAX_EXECUTION_ATTEMPTS = 3
# How long to wait before the second attempt, and before each later one, so a
# runner that refuses or is down does not use up every attempt at once.
RETRY_DELAYS = (timedelta(seconds=30), timedelta(minutes=2))
TOKEN_FAILURE = "could not issue an MCP token for the run"


GaveUp = Callable[[ProposalRecord], Awaitable[None]]


@dataclass(frozen=True)
class StartedRun:
    """A run that has just started, and the token its agent presents."""

    run: Run
    token: str = field(repr=False)


def grant_for(run: Run) -> Grant:
    """What the run's token allows: its kind, channel, thread, and for execution its proposal."""
    return Grant(
        run_id=run.id,
        kind=run.kind,
        channel_id=run.channel_id,
        thread_ts=run.thread_ts,
        proposal_id=run.proposal_id if run.kind is RunKind.EXECUTION else None,
    )


def token_ttl(run: Run, now: datetime) -> float:
    """Seconds the run's token lasts: to the deadline plus grace, within the cap."""
    remaining = (run.deadline - now).total_seconds() + GRACE_SECONDS
    return min(max(remaining, 1.0), float(MAX_TOKEN_TTL_SECONDS))


class RunLifecycle:
    """Starts and ends runs through the store, issuing and revoking their tokens."""

    def __init__(
        self,
        store: Store,
        registry: TokenRegistry,
        *,
        clock: Clock = utc_now,
        max_execution_attempts: int = MAX_EXECUTION_ATTEMPTS,
        on_gave_up: GaveUp | None = None,
    ) -> None:
        if max_execution_attempts < 1:
            raise ValueError("max_execution_attempts must be at least 1")
        self._store = store
        self._registry = registry
        self._now = clock
        self._max_attempts = max_execution_attempts
        self._on_gave_up = on_gave_up
        # proposal id -> (when to queue its next attempt, that run's timeout)
        self._retries: dict[str, tuple[datetime, int]] = {}

    async def start_next(
        self, *, max_running: int, kinds: Collection[RunKind] | None = None
    ) -> StartedRun | None:
        """Claim the next run that may start and issue its token, or return None.

        With ``kinds``, runs of other kinds are left queued.
        """
        run = await self._store.claim_next_run(max_running=max_running, kinds=kinds)
        if run is None:
            return None
        if run.kind is RunKind.EXECUTION:
            problem = await self._prepare_execution(run)
            if problem is not None:
                log.warning("run %s not started: %s", run.id, problem)
                await self._store.finish_run(run.id, RunStatus.FAILED, error=problem)
                return None
        try:
            token = self._registry.issue(grant_for(run), token_ttl(run, self._now()))
        except Exception:
            log.exception("run %s: %s", run.id, TOKEN_FAILURE)
            await self.finish(run.id, RunStatus.FAILED, error=TOKEN_FAILURE)
            raise
        log.info("run %s started (%s)", run.id, run.kind.value)
        return StartedRun(run, token)

    async def _prepare_execution(self, run: Run) -> str | None:
        """Make the run's proposal executing, so the agent's record_result is accepted.

        An approved proposal moves to executing. A reconciling one stays
        reconciling, because its run first checks for the hidden marker. Anything
        else, including losing the move to another run, is a reason not to start.
        Not one transaction with the claim: if JotFlow stops in between, the
        proposal is still approved and startup recovery queues it again.
        """
        proposal_id = run.proposal_id or ""
        record = await self._store.get_proposal(proposal_id)
        if record is None or record.channel_id != run.channel_id:
            return f"proposal {proposal_id} does not exist"
        if record.state is ProposalState.RECONCILING:
            # Restart recovery queues a run for every reconciling proposal, so
            # the limit is checked here too, not only when a run ends.
            if await self._attempts(record) >= self._max_attempts:
                await self._give_up(record)
                return f"proposal {proposal_id} reached its limit of {self._max_attempts} attempts"
            return None
        if record.state is ProposalState.APPROVED:
            moved = await try_transition(
                self._store,
                proposal_id,
                ProposalState.EXECUTING,
                expected={ProposalState.APPROVED},
            )
            if moved is not None:
                return None
            return f"proposal {proposal_id} stopped being approved before its run started"
        return f"proposal {proposal_id} is {record.state.value}, not approved or reconciling"

    async def finish(
        self, run_id: str, status: RunStatus, *, error: str | None = None
    ) -> Run | None:
        """End a run: revoke its token first, so it can act no more, then record the end.

        An execution run whose proposal is still executing did not record a
        result, whatever its exit, so the write may or may not have happened.
        Its proposal moves to reconciling, so the next attempt checks for the
        hidden marker before writing. While the proposal has had fewer than
        ``max_execution_attempts`` runs, a fresh one is queued; at the limit it
        fails, and ``on_gave_up`` is told (#30).

        Returns None if the run had already ended.
        """
        self._registry.revoke(run_id)
        ended = await self._store.finish_run(run_id, status, error=error)
        if ended is not None:
            log.info("run %s ended (%s)", run_id, status.value)
            if ended.kind is RunKind.EXECUTION and ended.proposal_id:
                await self._settle_proposal(ended.proposal_id, run_id)
                await self._retry_or_give_up(ended)
        return ended

    async def _attempts(self, record: ProposalRecord) -> int:
        """Execution runs for the proposal that have ended since it was last approved.

        A person approving again, such as Retry on a failed card (#27), starts
        a fresh count. A retry while reconciling keeps the approval, and the count.
        """
        since = record.approved_at
        runs = await self._store.thread_runs(record.channel_id, record.thread_ts)
        return sum(
            1
            for r in runs
            if r.kind is RunKind.EXECUTION
            and r.proposal_id == record.id
            and r.status not in ACTIVE_RUN_STATUSES
            and (since is None or r.created_at >= since)
        )

    async def _retry_or_give_up(self, ended: Run) -> None:
        record = await self._store.get_proposal(ended.proposal_id or "")
        if record is None or record.state is not ProposalState.RECONCILING:
            return
        attempts = await self._attempts(record)
        if attempts >= self._max_attempts:
            await self._give_up(record)
            return
        delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]
        self._retries[record.id] = (self._now() + delay, ended.timeout_seconds)
        log.info(
            "proposal %s: attempt %d of %d in %d s",
            record.id,
            attempts + 1,
            self._max_attempts,
            int(delay.total_seconds()),
        )

    async def queue_due_retries(self) -> list[str]:
        """Queue the execution runs whose retry delay has passed. Returns their ids.

        Retries wait in memory only. After a restart, recovery queues a run
        for every reconciling proposal at once, and the limit still holds.
        """
        now = self._now()
        queued: list[str] = []
        for proposal_id, (due, seconds) in sorted(self._retries.items()):
            if due > now:
                continue
            del self._retries[proposal_id]
            record = await self._store.get_proposal(proposal_id)
            if record is None or record.state is not ProposalState.RECONCILING:
                continue
            run = await self._store.enqueue_run(
                RunKind.EXECUTION,
                record.channel_id,
                record.thread_ts,
                proposal_id=record.id,
                timeout=timedelta(seconds=seconds),
            )
            queued.append(run.id)
        return queued

    def pending_retries(self) -> dict[str, datetime]:
        """Proposals waiting to be retried, and when."""
        return {proposal_id: due for proposal_id, (due, _) in self._retries.items()}

    async def _give_up(self, record: ProposalRecord) -> None:
        failed = await try_transition(
            self._store,
            record.id,
            ProposalState.FAILED,
            expected={ProposalState.RECONCILING},
        )
        if failed is None:
            return  # another path settled it first
        log.warning("proposal %s failed after %d execution attempts", record.id, self._max_attempts)
        if self._on_gave_up is not None:
            try:
                await self._on_gave_up(failed)
            except Exception:
                log.exception("proposal %s: could not post the give-up notice", record.id)

    async def _settle_proposal(self, proposal_id: str, run_id: str) -> None:
        moved = await try_transition(
            self._store,
            proposal_id,
            ProposalState.RECONCILING,
            expected={ProposalState.EXECUTING},
        )
        if moved is not None:
            log.warning(
                "run %s ended without a result; proposal %s is reconciling", run_id, proposal_id
            )

    async def expire_overdue(self) -> list[str]:
        """Time out every running run past its deadline. Returns their ids."""
        expired: list[str] = []
        for run in await self._store.overdue_runs():
            if await self.finish(run.id, RunStatus.TIMED_OUT) is not None:
                expired.append(run.id)
        return expired

    async def recover(self) -> Recovery:
        """At startup: settle interrupted runs and drop any token they still hold."""
        recovery = await self._store.recover_runs()
        for run_id in recovery.interrupted:
            self._registry.revoke(run_id)
        if recovery.interrupted:
            log.info(
                "recovered %d interrupted run(s); queued %d",
                len(recovery.interrupted),
                len(recovery.requeued),
            )
        return recovery
