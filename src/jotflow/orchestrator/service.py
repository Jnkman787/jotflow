"""Everything `jotflow run` runs besides the Slack connection (#55).

:class:`Orchestrator` opens the store, serves the whiteboard tools over MCP,
feeds accepted messages to the debouncer, and drives runs through
:class:`~jotflow.orchestrator.runs.RunLifecycle` and
:class:`~jotflow.orchestrator.launch.RunSupervisor`.

**Start order:**

1. Open the store.
2. Recover interrupted runs, before anything else touches runs.
3. Build the tools and start the MCP server.
4. Start the supervisor and the debouncer's flush loop.

**Stop order is the reverse:**

1. Stop the debouncer's loop.
2. Stop the supervisor, which tells the runners to kill live runs.
3. Revoke every token, so no agent can act while the MCP server shuts down.
4. Stop the MCP server.
5. Close the store.

Runs still running stay so in the store, and the next start's recovery marks
them interrupted and queues them again.

Agent runs are on only when ``[agent] preset`` is set in jotflow.toml and
JotFlow holds the drafting runner's secret. Otherwise nothing is queued, no
token is issued, and the MCP server refuses every call, as before. An
execution run without the exec runner's secret fails with a short notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from slack_sdk.web.async_client import AsyncWebClient

from jotflow.config import Settings
from jotflow.orchestrator.debounce import Debouncer
from jotflow.orchestrator.ingest import BotIdentity, Decision
from jotflow.orchestrator.launch import RunnerClient, RunSupervisor
from jotflow.orchestrator.packet import BoltSlackReader, SlackWorkSource
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.orchestrator.slack import SlackThreadPoster
from jotflow.policy import Policy
from jotflow.store import SqliteStore, Store
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.tokens import TokenRegistry
from jotflow.whiteboard.tools import ToolSpec
from jotflow.whiteboard.write_tools import write_tools

log = logging.getLogger("jotflow.orchestrator")

FLUSH_SECONDS = 1.0


class Server(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


ServerFactory = Callable[[Settings, TokenRegistry, Sequence[ToolSpec]], Server]
StoreOpener = Callable[[str], Awaitable[Store]]


async def open_sqlite(path: str) -> Store:
    return await SqliteStore.open(path)


def runs_off_reason(settings: Settings, policy: Policy) -> str | None:
    """Why agent runs are off, or None when they are on."""
    if policy.agent.preset is None:
        return "[agent] preset is not set in jotflow.toml"
    if settings.runner_secret is None:
        return "JOTFLOW_RUNNER_SECRET is not set"
    return None


class Orchestrator:
    """The store, the MCP server, the debouncer, and the run supervisor, in order."""

    def __init__(
        self,
        settings: Settings,
        policy: Policy,
        client: AsyncWebClient,
        identity: BotIdentity,
        *,
        make_server: ServerFactory,
        open_store: StoreOpener = open_sqlite,
        runner: RunnerClient | None = None,
        flush_seconds: float = FLUSH_SECONDS,
        tick_seconds: float | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._client = client
        self._identity = identity
        self._make_server = make_server
        self._open_store = open_store
        self._runner = runner
        self._flush_seconds = flush_seconds
        self._tick_seconds = tick_seconds
        self.registry = TokenRegistry()
        self.off_reason = runs_off_reason(settings, policy)
        self._store: Store | None = None
        self._server: Server | None = None
        self._debouncer: Debouncer | None = None
        self._supervisor: RunSupervisor | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def store(self) -> Store:
        if self._store is None:
            raise RuntimeError("the orchestrator has not started")
        return self._store

    @property
    def runs_on(self) -> bool:
        return self.off_reason is None

    async def start(self) -> None:
        """Start everything in order. On failure, stops what had started, then raises."""
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        self._store = await self._open_store(self._settings.database_path)
        lifecycle = RunLifecycle(self._store, self.registry)
        await lifecycle.recover()
        poster = SlackThreadPoster(self._client)
        source = SlackWorkSource(BoltSlackReader(self._client), self._store, bot=self._identity)
        tools = (
            *read_tools(self._store, self._policy, source),
            *write_tools(self._store, self._policy, poster),
        )
        self._server = self._make_server(self._settings, self.registry, tools)
        await self._server.start()
        if not self.runs_on:
            log.info("agent runs are off: %s", self.off_reason)
            return
        runner = self._runner or RunnerClient.from_settings(self._settings)
        self._runner = runner
        self._supervisor = RunSupervisor(
            lifecycle,
            self._store,
            runner,
            poster,
            max_running=self._policy.agent.max_concurrent_runs,
        )
        self._debouncer = Debouncer(self._store, self._policy)
        supervise = (
            self._supervisor.run(self._stop)
            if self._tick_seconds is None
            else self._supervisor.run(self._stop, interval=self._tick_seconds)
        )
        self._tasks = [
            asyncio.create_task(supervise, name="jotflow-supervisor"),
            asyncio.create_task(self._flush_loop(self._debouncer), name="jotflow-debouncer"),
        ]
        log.info("agent runs are on, up to %d at a time", self._policy.agent.max_concurrent_runs)

    async def note(self, decision: Decision) -> None:
        """Take one ingest decision. Does nothing while runs are off."""
        if self._debouncer is None:
            return
        try:
            await self._debouncer.note(decision)
        except Exception:
            log.exception("could not note a message for the debouncer")

    async def _flush_loop(self, debouncer: Debouncer) -> None:
        while not self._stop.is_set():
            try:
                await debouncer.flush()
            except Exception:
                log.exception("debouncer flush failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._flush_seconds)

    async def stop(self) -> None:
        """Stop in reverse order. Safe to call more than once, or after a failed start."""
        self._stop.set()
        # The supervisor's run() stops live runs on its runners as it exits.
        for task in self._tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        self._tasks = []
        revoked = self.registry.revoke_all()
        if revoked:
            log.info("revoked %d live run token(s) before stopping the MCP server", revoked)
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop()
        if self._runner is not None:
            await self._runner.aclose()
        if self._store is not None:
            store, self._store = self._store, None
            await store.close()
