"""Starting and ending agent runs, with their MCP tokens (#107).

Every run gets exactly one token while it may act, and loses it the moment it
stops. :class:`RunLifecycle` is the one place that starts and ends runs, so
that rule holds on every path: a normal finish, a timeout, and recovery at
startup.

A token lasts until the run's deadline plus :data:`GRACE_SECONDS`, never more
than :data:`~jotflow.whiteboard.tokens.MAX_TOKEN_TTL_SECONDS`. Run timeouts are
capped at the same constant (``MAX_RUN_TIMEOUT_SECONDS`` in the store), so no
run outlives its token.

Tokens are never stored or logged. The registry keeps only digests,
:class:`StartedRun` hides the token from ``repr``, and this module logs run ids
only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from jotflow.models import Run, RunKind, RunStatus
from jotflow.store.base import Clock, Recovery, Store, utc_now
from jotflow.whiteboard.tokens import MAX_TOKEN_TTL_SECONDS, Grant, TokenRegistry

log = logging.getLogger("jotflow.runs")

# Lets an agent's last tool call land just after its deadline, while the run
# is being timed out.
GRACE_SECONDS = 60
TOKEN_FAILURE = "could not issue an MCP token for the run"


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
    ) -> None:
        self._store = store
        self._registry = registry
        self._now = clock

    async def start_next(self, *, max_running: int) -> StartedRun | None:
        """Claim the next run that may start and issue its token, or return None."""
        run = await self._store.claim_next_run(max_running=max_running)
        if run is None:
            return None
        try:
            token = self._registry.issue(grant_for(run), token_ttl(run, self._now()))
        except Exception:
            log.exception("run %s: %s", run.id, TOKEN_FAILURE)
            await self._store.finish_run(run.id, RunStatus.FAILED, error=TOKEN_FAILURE)
            raise
        log.info("run %s started (%s)", run.id, run.kind.value)
        return StartedRun(run, token)

    async def finish(
        self, run_id: str, status: RunStatus, *, error: str | None = None
    ) -> Run | None:
        """End a run: revoke its token first, so it can act no more, then record the end.

        Returns None if the run had already ended.
        """
        self._registry.revoke(run_id)
        ended = await self._store.finish_run(run_id, status, error=error)
        if ended is not None:
            log.info("run %s ended (%s)", run_id, status.value)
        return ended

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
