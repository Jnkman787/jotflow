"""JotFlow's side of the agent runners (#21, D6).

Each agent service runs a runner (#54). JotFlow asks it to start a run with
exactly the run id, run kind, and run token, follows the run with ``GET``, and
stops it with ``DELETE``. Runs are routed by kind, with no fallback:

- drafting and question runs go to the ``agent`` runner, with
  ``JOTFLOW_RUNNER_SECRET``
- execution runs go to the ``agent-exec`` runner, with
  ``JOTFLOW_RUNNER_EXEC_SECRET``

A secret is only ever sent to its own runner. The client never follows a
redirect and ignores proxy settings from the environment, so neither the secret
nor the token can be carried to another host.

:class:`RunSupervisor` drives runs from start to end through
:class:`~jotflow.orchestrator.runs.RunLifecycle`, so every run loses its token
as it ends. When a run fails or times out, it posts a short notice in the run's
thread. The notice is fixed text naming the run id only. It never carries the
agent's output, exit codes, or the runner's replies, which may quote Slack
text or carry details only an admin should see.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from jotflow.config import Settings
from jotflow.models import Run, RunKind, RunStatus
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.store.base import Store
from jotflow.whiteboard.write_tools import PostError, ThreadPoster

log = logging.getLogger("jotflow.launch")

REQUEST_TIMEOUT_SECONDS = 10.0
TICK_SECONDS = 2.0
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

FAILED_NOTICE = (
    "JotFlow's agent run for this thread did not finish (run `{run_id}`). "
    "An admin can find the reason in JotFlow's logs."
)
TIMED_OUT_NOTICE = (
    "JotFlow's agent run for this thread ran out of time and was stopped (run `{run_id}`)."
)

# Fixed reasons, stored on the run and logged. Never the runner's reply.
NO_RUNNER = "no runner secret is configured for this run kind"
UNREACHABLE = "the runner could not be reached"
LOST = "the runner lost the run"
NONZERO_EXIT = "the agent exited with a non-zero code"
STOPPED = "the runner stopped the run"
NOT_STARTED = "the runner could not start the agent"
_REFUSALS = {
    400: "the runner refused the request as malformed (400)",
    401: "the runner refused JotFlow's secret (401)",
    403: "the runner does not serve this run kind (403)",
    409: "the runner already has this run (409)",
    413: "the runner refused the request as too large (413)",
    429: "the runner is at its limit of live runs (429)",
    503: "the runner has no GitHub token for this run kind (503)",
}


class RunnerError(Exception):
    """A runner call did not succeed. The message is one of the fixed reasons."""


def _refused(status: int) -> RunnerError:
    return RunnerError(_REFUSALS.get(status, f"the runner answered {status}"))


class RunnerState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    KILLED = "killed"
    FAILED = "failed"


LIVE_STATES = frozenset({RunnerState.STARTING, RunnerState.RUNNING})


class RunnerStatus(BaseModel):
    """The runner's view of a run, validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    state: RunnerState
    exit_code: int | None = None


class RunnerInfo(BaseModel):
    """What a runner says it starts, validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    preset: str = Field(max_length=64)
    kinds: tuple[RunKind, ...] = Field(max_length=len(RunKind))


class RunnerInfoWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    preset: str = Field(max_length=64)
    kinds: list[str] = Field(max_length=len(RunKind))


@dataclass(frozen=True)
class RunnerEndpoint:
    """One runner: where it listens and the secret only it receives."""

    url: str
    secret: str = field(repr=False)


class RunnerClient:
    """Calls the runner that serves each run's kind."""

    def __init__(
        self,
        *,
        conversation: RunnerEndpoint | None,
        execution: RunnerEndpoint | None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._conversation = conversation
        self._execution = execution
        self._http = httpx2.AsyncClient(
            transport=transport,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> RunnerClient:
        def endpoint(url: str, secret: SecretStr | None) -> RunnerEndpoint | None:
            return RunnerEndpoint(url, secret.get_secret_value()) if secret is not None else None

        return cls(
            conversation=endpoint(settings.runner_url, settings.runner_secret),
            execution=endpoint(settings.runner_exec_url, settings.runner_exec_secret),
            transport=transport,
        )

    def endpoint_for(self, kind: RunKind) -> RunnerEndpoint | None:
        """The runner for ``kind``. Execution never falls back to the other, nor the reverse."""
        return self._execution if kind is RunKind.EXECUTION else self._conversation

    def serves(self, kind: RunKind) -> bool:
        return self.endpoint_for(kind) is not None

    async def launch(self, run_id: str, kind: RunKind, token: str) -> None:
        """Ask the runner to start the run. Raises :class:`RunnerError` unless it started."""
        response = await self._call(
            "POST",
            kind,
            "/runs",
            json={"run_id": _checked(run_id), "kind": kind.value, "token": token},
        )
        if response.status_code != 202:
            raise _refused(response.status_code)

    async def status(self, run_id: str, kind: RunKind) -> RunnerStatus | None:
        """The run's state on its runner, or None if the runner does not know it."""
        response = await self._call("GET", kind, f"/runs/{_checked(run_id)}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            status = RunnerStatus.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a run status") from None
        if status.run_id != run_id:
            raise RunnerError("the runner's reply was for another run")
        return status

    async def info(self, kind: RunKind) -> RunnerInfo:
        """What the runner for ``kind`` starts: its preset and the kinds it serves."""
        response = await self._call("GET", kind, "/info")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            # Strict except for the kinds, which arrive as JSON strings.
            raw = RunnerInfoWire.model_validate_json(response.content)
            return RunnerInfo(preset=raw.preset, kinds=tuple(RunKind(k) for k in raw.kinds))
        except (ValidationError, ValueError):
            raise RunnerError("the runner's reply was not its info") from None

    async def stop(self, run_id: str, kind: RunKind) -> None:
        """Ask the runner to kill the run. A run it does not know is already gone."""
        response = await self._call("DELETE", kind, f"/runs/{_checked(run_id)}")
        if response.status_code not in (200, 404):
            raise _refused(response.status_code)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, kind: RunKind, path: str, *, json: object = None
    ) -> httpx2.Response:
        endpoint = self.endpoint_for(kind)
        if endpoint is None:
            raise RunnerError(NO_RUNNER)
        try:
            return await self._http.request(
                method,
                endpoint.url + path,
                json=json,
                headers={"Authorization": f"Bearer {endpoint.secret}"},
            )
        except httpx2.HTTPError:
            # Never the exception text: it can name the request.
            raise RunnerError(UNREACHABLE) from None


def _checked(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise RunnerError("the run id is not one a runner accepts")
    return run_id


def notice_for(run: Run) -> str | None:
    """The thread notice for a run that has ended, or None if it succeeded."""
    if run.status is RunStatus.TIMED_OUT:
        return TIMED_OUT_NOTICE.format(run_id=run.id)
    if run.status is RunStatus.FAILED:
        return FAILED_NOTICE.format(run_id=run.id)
    return None


def outcome(status: RunnerStatus) -> tuple[RunStatus, str | None] | None:
    """How a run ends, given its runner's state, or None while it is still live."""
    if status.state in LIVE_STATES:
        return None
    if status.state is RunnerState.EXITED:
        if status.exit_code == 0:
            return RunStatus.SUCCEEDED, None
        return RunStatus.FAILED, NONZERO_EXIT
    if status.state is RunnerState.TIMED_OUT:
        return RunStatus.TIMED_OUT, None
    if status.state is RunnerState.KILLED:
        return RunStatus.FAILED, STOPPED
    return RunStatus.FAILED, NOT_STARTED


class RunSupervisor:
    """Starts queued runs on their runners and follows each to its end.

    One :meth:`tick` times out overdue runs, then polls live ones, then starts
    any that may start. :meth:`run` repeats it until stopped.
    """

    def __init__(
        self,
        lifecycle: RunLifecycle,
        store: Store,
        client: RunnerClient,
        poster: ThreadPoster,
        *,
        max_running: int,
        kinds: Collection[RunKind] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._kinds = frozenset(kinds) if kinds is not None else None
        self._store = store
        self._client = client
        self._poster = poster
        self._max_running = max_running
        self._live: dict[str, Run] = {}

    def tracked(self) -> list[str]:
        return list(self._live)

    async def tick(self) -> None:
        await self._lifecycle.queue_due_retries()
        await self._expire()
        await self._poll()
        await self._launch()

    async def run(self, stop: asyncio.Event, *, interval: float = TICK_SECONDS) -> None:
        """Tick every ``interval`` seconds until ``stop`` is set, then stop live runs."""
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.exception("run supervisor tick failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval)
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop every live run on its runner, for JotFlow's shutdown.

        The runs stay running in the store; startup recovery settles them, as
        after a crash. Their tokens live only in memory and end with JotFlow.
        """
        for run in list(self._live.values()):
            await self._stop(run)
        self._live.clear()

    async def _expire(self) -> None:
        for run_id in await self._lifecycle.expire_overdue():
            run = self._live.pop(run_id, None)
            if run is not None:
                await self._stop(run)
            ended = await self._store.get_run(run_id)
            if ended is not None:
                log.warning("run %s timed out", run_id)
                await self._notify(ended)

    async def _poll(self) -> None:
        for run in list(self._live.values()):
            try:
                status = await self._client.status(run.id, run.kind)
            except RunnerError as exc:
                # Its deadline still bounds it.
                log.warning("run %s: could not check its state (%s)", run.id, exc)
                continue
            if status is None:
                await self._end(run, RunStatus.FAILED, LOST)
                continue
            ended = outcome(status)
            if ended is not None:
                await self._end(run, *ended)

    async def _launch(self) -> None:
        while len(self._live) < self._max_running:
            try:
                started = await self._lifecycle.start_next(
                    max_running=self._max_running, kinds=self._kinds
                )
            except Exception:
                log.exception("could not start the next run")
                return
            if started is None:
                return
            run = started.run
            try:
                await self._client.launch(run.id, run.kind, started.token)
            except RunnerError as exc:
                await self._end(run, RunStatus.FAILED, str(exc))
                if str(exc) == UNREACHABLE:
                    # The request may have arrived before the connection broke.
                    # The token is already revoked; this frees the runner's slot.
                    await self._stop(run)
                continue
            self._live[run.id] = run
            log.info("run %s launched on its runner (%s)", run.id, run.kind.value)

    async def _end(self, run: Run, status: RunStatus, error: str | None) -> None:
        self._live.pop(run.id, None)
        if error is not None:
            log.warning("run %s %s: %s", run.id, status.value, error)
        ended = await self._lifecycle.finish(run.id, status, error=error)
        if ended is not None:
            await self._notify(ended)

    async def _stop(self, run: Run) -> None:
        try:
            await self._client.stop(run.id, run.kind)
        except RunnerError as exc:
            log.warning("run %s: could not stop it on its runner (%s)", run.id, exc)

    async def _notify(self, run: Run) -> None:
        text = notice_for(run)
        if text is None:
            return
        try:
            await self._poster.post(run.channel_id, run.thread_ts, text)
        except PostError as exc:
            log.warning("run %s: could not post the failure notice (%s)", run.id, exc)
