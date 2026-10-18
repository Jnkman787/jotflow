"""JotFlow's runner client and run supervisor (#21)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from jotflow.adapters.runner import Runner, RunnerConfig, build_runner_app
from jotflow.config import Settings
from jotflow.models import ProposalState, RunKind, RunStatus
from jotflow.orchestrator.launch import (
    FAILED_NOTICE,
    LOST,
    NO_RUNNER,
    NONZERO_EXIT,
    STOPPED,
    TIMED_OUT_NOTICE,
    UNREACHABLE,
    RunnerClient,
    RunnerEndpoint,
    RunnerError,
    RunnerState,
    RunnerStatus,
    RunSupervisor,
    notice_for,
    outcome,
)
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.store import MemoryStore
from jotflow.store.base import MAX_RUN_TIMEOUT_SECONDS
from jotflow.whiteboard.server import McpServer, build_app
from jotflow.whiteboard.tokens import TokenRegistry
from jotflow.whiteboard.write_tools import PostError, RecordResultArgs, WriteTools
from tests.test_runner import ENVIRON, SECRET, _free_port, config, recorded
from tests.test_runs import CH, ISSUE_URL, POLICY, THREAD, Clock, _approved

EXEC_SECRET = "e" * 40
TOKEN = "jfr_" + "t" * 43
SLACK = {"slack_bot_token": "xoxb-x", "slack_app_token": "xapp-x"}


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **SLACK, **overrides)  # type: ignore[call-arg]


# Settings


def test_runner_settings_default_to_the_compose_names_and_no_secrets() -> None:
    made = settings()
    assert (made.runner_url, made.runner_exec_url) == (
        "http://agent:8766",
        "http://agent-exec:8766",
    )
    assert made.runner_secret is None and made.runner_exec_secret is None


def test_runner_settings_read_their_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOTFLOW_RUNNER_SECRET", SECRET)
    monkeypatch.setenv("JOTFLOW_RUNNER_EXEC_SECRET", EXEC_SECRET)
    monkeypatch.setenv("JOTFLOW_RUNNER_EXEC_URL", "http://runner-exec:9000/")
    made = settings()
    assert made.runner_secret is not None and made.runner_secret.get_secret_value() == SECRET
    assert made.runner_exec_secret is not None
    assert made.runner_exec_secret.get_secret_value() == EXEC_SECRET
    assert made.runner_exec_url == "http://runner-exec:9000"
    assert SECRET not in repr(made) and EXEC_SECRET not in repr(made)


def test_an_empty_secret_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOTFLOW_RUNNER_EXEC_SECRET", "")
    assert settings().runner_exec_secret is None


def test_the_two_runner_secrets_must_differ() -> None:
    with pytest.raises(ValidationError, match="must differ") as caught:
        settings(runner_secret=SECRET, runner_exec_secret=SECRET)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///run/agent",
        "agent:8766",
        "http://user:pw@agent:8766",
        "http://agent:8766/runs",
        "http://agent:8766?x=1",
        "http://agent:8766#x",
        "http://:8766",
    ],
)
def test_a_runner_url_is_only_a_scheme_host_and_port(url: str) -> None:
    with pytest.raises(ValidationError, match="only a host and port"):
        settings(runner_url=url)


# Routing: each secret goes only to its own runner


class Recorder:
    """A transport that records requests and answers with a fixed reply."""

    def __init__(self, status: int = 202, body: Any = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.body = body

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body if self.body is not None else {})


def recording_client(recorder: Recorder, *, execution: bool = True) -> RunnerClient:
    return RunnerClient(
        conversation=RunnerEndpoint("http://agent:8766", SECRET),
        execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET) if execution else None,
        transport=httpx2.MockTransport(recorder),
    )


@pytest.mark.parametrize(
    ("kind", "host", "secret", "other"),
    [
        (RunKind.DRAFTING, "agent", SECRET, EXEC_SECRET),
        (RunKind.QUESTION, "agent", SECRET, EXEC_SECRET),
        (RunKind.EXECUTION, "agent-exec", EXEC_SECRET, SECRET),
    ],
)
async def test_each_kind_goes_to_its_own_runner_with_its_own_secret(
    kind: RunKind, host: str, secret: str, other: str
) -> None:
    recorder = Recorder()
    client = recording_client(recorder)
    await client.launch("run-1", kind, TOKEN)
    recorder.status, recorder.body = 200, {"run_id": "run-1", "state": "running"}
    await client.status("run-1", kind)
    await client.stop("run-1", kind)
    await client.aclose()
    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("POST", "/runs"),
        ("GET", "/runs/run-1"),
        ("DELETE", "/runs/run-1"),
    ]
    for request in recorder.requests:
        assert request.url.host == host and request.url.port == 8766
        assert request.headers["authorization"] == f"Bearer {secret}"
        assert other not in str(request.headers) and other not in request.content.decode()
    assert json.loads(recorder.requests[0].content) == {
        "run_id": "run-1",
        "kind": kind.value,
        "token": TOKEN,
    }


async def test_a_kind_without_a_secret_sends_nothing_and_never_falls_back() -> None:
    recorder = Recorder()
    client = recording_client(recorder, execution=False)
    assert client.serves(RunKind.DRAFTING) and not client.serves(RunKind.EXECUTION)
    with pytest.raises(RunnerError, match=NO_RUNNER):
        await client.launch("run-1", RunKind.EXECUTION, TOKEN)
    assert recorder.requests == []


def test_from_settings_pairs_each_url_with_its_secret() -> None:
    client = RunnerClient.from_settings(
        settings(runner_secret=SECRET, runner_exec_secret=EXEC_SECRET)
    )
    assert client.endpoint_for(RunKind.DRAFTING) == RunnerEndpoint("http://agent:8766", SECRET)
    assert client.endpoint_for(RunKind.EXECUTION) == RunnerEndpoint(
        "http://agent-exec:8766", EXEC_SECRET
    )
    assert SECRET not in repr(client.endpoint_for(RunKind.DRAFTING))
    only = RunnerClient.from_settings(settings(runner_secret=SECRET))
    assert only.endpoint_for(RunKind.EXECUTION) is None


async def test_a_redirect_is_not_followed() -> None:
    seen: list[httpx2.Request] = []

    def redirect(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(307, headers={"Location": "http://elsewhere:80/runs"})

    client = RunnerClient(
        conversation=RunnerEndpoint("http://agent:8766", SECRET),
        execution=None,
        transport=httpx2.MockTransport(redirect),
    )
    with pytest.raises(RunnerError, match="answered 307"):
        await client.launch("run-1", RunKind.DRAFTING, TOKEN)
    assert len(seen) == 1


def test_the_client_ignores_proxies_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    client = RunnerClient(conversation=RunnerEndpoint("http://agent:8766", SECRET), execution=None)
    assert client._http.trust_env is False


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "secret"),
        (403, "kind"),
        (409, "already"),
        (429, "limit"),
        (503, "GitHub"),
        (500, "500"),
    ],
)
async def test_a_refused_launch_has_a_fixed_reason(status: int, reason: str) -> None:
    client = recording_client(Recorder(status, {"error": "xoxb-quoted-from-somewhere"}))
    with pytest.raises(RunnerError, match=reason) as caught:
        await client.launch("run-1", RunKind.DRAFTING, TOKEN)
    assert "xoxb" not in str(caught.value) and TOKEN not in str(caught.value)


async def test_an_unreachable_runner_names_neither_secret_nor_token() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot reach {request.url} {request.headers}")

    client = RunnerClient(
        conversation=RunnerEndpoint("http://agent:8766", SECRET),
        execution=None,
        transport=httpx2.MockTransport(broken),
    )
    with pytest.raises(RunnerError) as caught:
        await client.launch("run-1", RunKind.DRAFTING, TOKEN)
    assert str(caught.value) == UNREACHABLE
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize(
    "body",
    [
        {"run_id": "run-2", "state": "running"},
        {"run_id": "run-1", "state": "sleeping"},
        {"run_id": "run-1", "state": "exited", "exit_code": "0"},
        {"run_id": "run-1", "state": "exited", "extra": 1},
        ["run-1"],
    ],
)
async def test_a_status_reply_is_validated_strictly(body: Any) -> None:
    client = recording_client(Recorder(200, body))
    with pytest.raises(RunnerError, match="reply"):
        await client.status("run-1", RunKind.DRAFTING)


async def test_unknown_runs_and_bad_run_ids() -> None:
    recorder = Recorder(404, {"error": "no such run"})
    client = recording_client(recorder)
    assert await client.status("run-1", RunKind.DRAFTING) is None
    await client.stop("run-1", RunKind.DRAFTING)
    with pytest.raises(RunnerError, match="500"):
        await recording_client(Recorder(500)).status("run-1", RunKind.DRAFTING)
    with pytest.raises(RunnerError, match="401"):
        await recording_client(Recorder(401)).stop("run-1", RunKind.DRAFTING)
    for bad in ("../runs", "run 1", "a" * 65, "run/1"):
        with pytest.raises(RunnerError, match="run id"):
            await client.status(bad, RunKind.DRAFTING)
    assert len(recorder.requests) == 2


# Against the real runner app


class Runners(httpx2.AsyncBaseTransport):
    """Both runner apps in process, chosen by host, as Compose's network would."""

    def __init__(self, apps: dict[str, Any]) -> None:
        self._transports = {host: httpx2.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host = f"{request.url.host}:{request.url.port}"
        return await self._transports[host].handle_async_request(request)


def runner_pair(tmp_path: Path, mode: str = "exit") -> tuple[RunnerClient, Runner, Runner]:
    convo_dir, exec_dir = tmp_path / "agent", tmp_path / "agent-exec"
    convo_dir.mkdir()
    exec_dir.mkdir()
    convo_cfg = config(convo_dir, mode)
    exec_cfg: RunnerConfig = config(
        exec_dir,
        mode,
        execution=True,
        secret=EXEC_SECRET,
        allowed_hosts=frozenset({"agent-exec:8766"}),
    )
    convo = Runner(convo_cfg, environ=ENVIRON)
    execute = Runner(exec_cfg, environ=ENVIRON)
    transport = Runners(
        {
            "agent:8766": build_runner_app(convo, convo_cfg),
            "agent-exec:8766": build_runner_app(execute, exec_cfg),
        }
    )
    client = RunnerClient(
        conversation=RunnerEndpoint("http://agent:8766", SECRET),
        execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET),
        transport=transport,
    )
    return client, convo, execute


async def until_ended(client: RunnerClient, run_id: str, kind: RunKind) -> RunnerStatus:
    for _ in range(500):
        status = await client.status(run_id, kind)
        assert status is not None
        if outcome(status) is not None:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError("the run did not end")


async def test_the_real_runners_accept_their_own_kinds_only(tmp_path: Path) -> None:
    client, convo, execute = runner_pair(tmp_path)
    await client.launch("run-1", RunKind.DRAFTING, TOKEN)
    ended = await until_ended(client, "run-1", RunKind.DRAFTING)
    assert (ended.state, ended.exit_code) == (RunnerState.EXITED, 0)
    with pytest.raises(RunnerError, match="409"):
        await client.launch("run-1", RunKind.DRAFTING, TOKEN)
    await client.launch("run-2", RunKind.EXECUTION, TOKEN)
    await until_ended(client, "run-2", RunKind.EXECUTION)
    assert convo.get("run-2") is None and execute.get("run-1") is None
    # A drafting secret on the exec runner is refused, and the reverse.
    swapped = RunnerClient(
        conversation=RunnerEndpoint("http://agent-exec:8766", SECRET),
        execution=RunnerEndpoint("http://agent:8766", EXEC_SECRET),
        transport=client._http._transport,
    )
    with pytest.raises(RunnerError, match="401"):
        await swapped.launch("run-3", RunKind.DRAFTING, TOKEN)
    with pytest.raises(RunnerError, match="401"):
        await swapped.launch("run-3", RunKind.EXECUTION, TOKEN)
    await convo.close()
    await execute.close()


async def test_stop_kills_a_run_on_the_real_runner(tmp_path: Path) -> None:
    client, convo, execute = runner_pair(tmp_path, "sleep")
    await client.launch("run-1", RunKind.QUESTION, TOKEN)
    for _ in range(500):
        if (tmp_path / "agent" / "out.json").exists():
            break
        await asyncio.sleep(0.02)
    await client.stop("run-1", RunKind.QUESTION)
    status = await client.status("run-1", RunKind.QUESTION)
    assert status is not None and status.state is RunnerState.KILLED
    assert outcome(status) == (RunStatus.FAILED, STOPPED)
    await convo.close()
    await execute.close()


# Outcomes and notices


@pytest.mark.parametrize(
    ("state", "code", "expected"),
    [
        ("starting", None, None),
        ("running", None, None),
        ("exited", 0, (RunStatus.SUCCEEDED, None)),
        ("exited", 3, (RunStatus.FAILED, NONZERO_EXIT)),
        ("exited", None, (RunStatus.FAILED, NONZERO_EXIT)),
        ("timed_out", -9, (RunStatus.TIMED_OUT, None)),
        ("killed", -15, (RunStatus.FAILED, STOPPED)),
        ("failed", None, (RunStatus.FAILED, "could not start")),
    ],
)
def test_runner_states_map_to_run_outcomes(
    state: str, code: int | None, expected: tuple[RunStatus, str | None] | None
) -> None:
    got = outcome(RunnerStatus(run_id="r", state=RunnerState(state), exit_code=code))
    if expected is None or expected[1] is None:
        assert got == expected
    else:
        assert got is not None and got[0] is expected[0] and expected[1] in (got[1] or "")


# The supervisor


class Poster:
    def __init__(self, fail: bool = False) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.fail = fail

    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        if self.fail:
            raise PostError("channel_not_found")
        self.posts.append((channel_id, thread_ts, text))
        return "1.0"


class FakeRunner:
    """A scripted runner: launch replies, then one status per poll."""

    def __init__(self) -> None:
        self.launch_status = 202
        self.states: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.unreachable = False
        self.tokens: dict[str, str] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append((request.method, request.url.path))
        if self.unreachable:
            raise httpx2.ConnectError("down")
        if request.method == "POST":
            body = json.loads(request.content)
            self.tokens[body["run_id"]] = body["token"]
            return httpx2.Response(self.launch_status, json={})
        run_id = request.url.path.rsplit("/", 1)[1]
        if request.method == "DELETE":
            return httpx2.Response(200, json={"run_id": run_id, "state": "killed"})
        script = self.states.get(run_id) or ["running"]
        step = script.pop(0) if len(script) > 1 else script[0]
        if step == 404:
            return httpx2.Response(404, json={})
        state, _, code = str(step).partition(":")
        body = {"run_id": run_id, "state": state, "exit_code": int(code) if code else None}
        return httpx2.Response(200, json=body)


class Rig:
    def __init__(self, clock: Clock, *, max_running: int = 2, poster: Poster | None = None) -> None:
        self.clock = clock
        self.store = MemoryStore(clock=clock)
        self.registry = TokenRegistry(clock=clock.mono)
        self.lifecycle = RunLifecycle(self.store, self.registry, clock=clock)
        self.runner = FakeRunner()
        self.poster = poster or Poster()
        self.client = RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", SECRET),
            execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET),
            transport=httpx2.MockTransport(self.runner),
        )
        self.supervisor = RunSupervisor(
            self.lifecycle, self.store, self.client, self.poster, max_running=max_running
        )

    async def enqueue(self, thread_ts: str = THREAD, **kwargs: Any) -> str:
        run = await self.store.enqueue_run(RunKind.DRAFTING, CH, thread_ts, **kwargs)
        return run.id

    async def status(self, run_id: str) -> RunStatus:
        run = await self.store.get_run(run_id)
        assert run is not None
        return run.status


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    made = Rig(Clock())
    yield made
    await made.client.aclose()


async def test_a_run_launches_and_succeeds_with_no_notice(rig: Rig) -> None:
    run_id = await rig.enqueue()
    rig.runner.states[run_id] = ["running", "exited:0"]
    await rig.supervisor.tick()
    assert rig.supervisor.tracked() == [run_id]
    assert await rig.status(run_id) is RunStatus.RUNNING
    assert rig.registry.active() == 1
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.RUNNING
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.SUCCEEDED
    assert rig.supervisor.tracked() == [] and rig.registry.active() == 0
    assert rig.poster.posts == []


async def test_a_non_zero_exit_fails_the_run_with_a_fixed_notice(rig: Rig) -> None:
    run_id = await rig.enqueue()
    rig.runner.states[run_id] = ["exited:3"]
    await rig.supervisor.tick()
    await rig.supervisor.tick()
    run = await rig.store.get_run(run_id)
    assert run is not None and run.status is RunStatus.FAILED and run.error == NONZERO_EXIT
    assert rig.registry.active() == 0
    assert rig.poster.posts == [(CH, THREAD, FAILED_NOTICE.format(run_id=run_id))]
    await rig.supervisor.tick()
    assert len(rig.poster.posts) == 1


@pytest.mark.parametrize(("status", "reason"), [(429, "limit"), (403, "kind"), (409, "already")])
async def test_a_refused_launch_fails_the_run_and_revokes_its_token(
    rig: Rig, status: int, reason: str
) -> None:
    rig.runner.launch_status = status
    run_id = await rig.enqueue()
    await rig.supervisor.tick()
    run = await rig.store.get_run(run_id)
    assert run is not None and run.status is RunStatus.FAILED
    assert run.error is not None and reason in run.error
    assert rig.registry.active() == 0 and rig.supervisor.tracked() == []
    assert rig.poster.posts == [(CH, THREAD, FAILED_NOTICE.format(run_id=run_id))]


async def test_an_unreachable_runner_at_launch_fails_the_run_and_tries_to_stop_it(
    rig: Rig,
) -> None:
    rig.runner.unreachable = True
    run_id = await rig.enqueue()
    await rig.supervisor.tick()
    run = await rig.store.get_run(run_id)
    assert run is not None and run.status is RunStatus.FAILED and run.error == UNREACHABLE
    assert rig.registry.active() == 0
    assert rig.runner.calls == [("POST", "/runs"), ("DELETE", f"/runs/{run_id}")]


async def test_a_run_the_runner_lost_fails(rig: Rig) -> None:
    run_id = await rig.enqueue()
    rig.runner.states[run_id] = [404]
    await rig.supervisor.tick()
    await rig.supervisor.tick()
    run = await rig.store.get_run(run_id)
    assert run is not None and run.status is RunStatus.FAILED and run.error == LOST


async def test_a_runner_side_timeout_times_the_run_out(rig: Rig) -> None:
    run_id = await rig.enqueue()
    rig.runner.states[run_id] = ["timed_out:-9"]
    await rig.supervisor.tick()
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.TIMED_OUT
    assert rig.poster.posts == [(CH, THREAD, TIMED_OUT_NOTICE.format(run_id=run_id))]


async def test_an_overdue_run_loses_its_token_then_is_killed_and_noticed(rig: Rig) -> None:
    run_id = await rig.enqueue(timeout=timedelta(minutes=5))
    await rig.supervisor.tick()
    token_holder = rig.registry.active()
    assert token_holder == 1
    rig.clock.advance(5 * 60 + 1)
    rig.runner.calls.clear()
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.TIMED_OUT
    assert rig.registry.active() == 0
    assert rig.runner.calls[0] == ("DELETE", f"/runs/{run_id}")
    assert ("GET", f"/runs/{run_id}") not in rig.runner.calls
    assert rig.supervisor.tracked() == []
    assert rig.poster.posts == [(CH, THREAD, TIMED_OUT_NOTICE.format(run_id=run_id))]


async def test_an_unreachable_runner_while_polling_leaves_the_run_to_its_deadline(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = await rig.enqueue(timeout=timedelta(minutes=5))
    await rig.supervisor.tick()
    rig.runner.unreachable = True
    with caplog.at_level(logging.WARNING, logger="jotflow.launch"):
        await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.RUNNING
    assert "could not check its state" in caplog.text
    rig.clock.advance(5 * 60 + 1)
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.TIMED_OUT
    assert rig.registry.active() == 0
    assert SECRET not in caplog.text and TOKEN not in caplog.text


async def test_launches_stop_at_the_cap(rig: Rig) -> None:
    ids = [await rig.enqueue(f"1760781600.00010{i}") for i in range(3)]
    await rig.supervisor.tick()
    assert sorted(rig.supervisor.tracked()) == sorted(ids[:2])
    assert await rig.status(ids[2]) is RunStatus.QUEUED
    rig.runner.states[ids[0]] = ["exited:0"]
    await rig.supervisor.tick()
    assert await rig.status(ids[0]) is RunStatus.SUCCEEDED
    assert ids[2] in rig.supervisor.tracked()


async def test_an_execution_run_goes_to_agent_exec(rig: Rig) -> None:
    await _approved(rig.store)
    hosts: list[str] = []
    original = rig.runner.__call__

    def watch(request: httpx2.Request) -> httpx2.Response:
        hosts.append(str(request.url.host))
        assert request.headers["authorization"] == f"Bearer {EXEC_SECRET}"
        return original(request)

    rig.client._http._transport = httpx2.MockTransport(watch)
    await rig.store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    await rig.supervisor.tick()
    assert hosts == ["agent-exec"]


async def test_a_failed_notice_post_is_logged_not_raised(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(clock, poster=Poster(fail=True))
    rig.runner.launch_status = 429
    run_id = await rig.enqueue()
    with caplog.at_level(logging.WARNING, logger="jotflow.launch"):
        await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.FAILED
    assert "could not post the failure notice (channel_not_found)" in caplog.text
    await rig.client.aclose()


async def test_the_notice_is_fixed_text_whatever_the_runner_says(rig: Rig) -> None:
    run_id = await rig.enqueue()
    rig.runner.launch_status = 503
    await rig.supervisor.tick()
    ((_, _, text),) = rig.poster.posts
    assert text == FAILED_NOTICE.format(run_id=run_id)
    run = await rig.store.get_run(run_id)
    assert run is not None
    assert notice_for(run) == text
    assert notice_for(run.model_copy(update={"status": RunStatus.SUCCEEDED})) is None


async def test_close_stops_every_live_run_and_leaves_them_for_recovery(rig: Rig) -> None:
    ids = [await rig.enqueue(f"1760781600.00010{i}") for i in range(2)]
    await rig.supervisor.tick()
    rig.runner.calls.clear()
    await rig.supervisor.close()
    assert sorted(rig.runner.calls) == sorted(("DELETE", f"/runs/{i}") for i in ids)
    assert rig.supervisor.tracked() == []
    assert {await rig.status(i) for i in ids} == {RunStatus.RUNNING}


async def test_run_ticks_until_stopped_then_closes(rig: Rig) -> None:
    run_id = await rig.enqueue()
    stop = asyncio.Event()
    task = asyncio.create_task(rig.supervisor.run(stop, interval=0.01))
    for _ in range(200):
        if rig.supervisor.tracked():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert ("DELETE", f"/runs/{run_id}") in rig.runner.calls
    assert rig.supervisor.tracked() == []


async def test_run_survives_a_failing_tick(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticks = 0

    async def broken() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            stop.set()
        raise RuntimeError("store is down")

    monkeypatch.setattr(rig.supervisor, "tick", broken)
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="jotflow.launch"):
        await rig.supervisor.run(stop, interval=0.001)
    assert ticks == 3 and "tick failed" in caplog.text


async def test_a_token_failure_at_start_stops_launching_for_the_tick(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(clock)
    rig.registry = TokenRegistry(clock=clock.mono, max_live=0)
    rig.lifecycle = RunLifecycle(rig.store, rig.registry, clock=clock)
    rig.supervisor = RunSupervisor(rig.lifecycle, rig.store, rig.client, rig.poster, max_running=2)
    run_id = await rig.enqueue()
    with caplog.at_level(logging.ERROR, logger="jotflow.launch"):
        await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.FAILED
    assert "could not start the next run" in caplog.text
    assert rig.runner.calls == []
    await rig.client.aclose()


# End to end in process: the real runner, the real MCP server, a stub agent


async def test_a_supervised_run_reaches_mcp_and_succeeds(tmp_path: Path) -> None:
    port = _free_port()
    store = MemoryStore()
    registry = TokenRegistry()
    lifecycle = RunLifecycle(store, registry)
    server = McpServer(build_app(port=port, tokens=registry), "127.0.0.1", port)
    convo_cfg = config(tmp_path, "mcp", mcp_url=f"http://127.0.0.1:{port}/mcp")
    convo = Runner(convo_cfg, environ=ENVIRON)
    client = RunnerClient(
        conversation=RunnerEndpoint("http://agent:8766", SECRET),
        execution=None,
        transport=Runners({"agent:8766": build_runner_app(convo, convo_cfg)}),
    )
    poster = Poster()
    supervisor = RunSupervisor(lifecycle, store, client, poster, max_running=2)
    run = await store.enqueue_run(RunKind.DRAFTING, CH, THREAD)
    await server.start()
    try:
        await supervisor.tick()
        assert supervisor.tracked() == [run.id]
        for _ in range(500):
            await supervisor.tick()
            if not supervisor.tracked():
                break
            await asyncio.sleep(0.02)
    finally:
        await server.stop()
        await convo.close()
        await client.aclose()
    ended = await store.get_run(run.id)
    assert ended is not None and ended.status is RunStatus.SUCCEEDED
    seen = recorded(tmp_path)
    assert seen["mcp_status"] == 200
    assert seen["config"]["mcpServers"]["jotflow"]["headers"]["Authorization"].startswith(
        "Bearer jfr_"
    )
    assert registry.active() == 0 and poster.posts == []


def test_the_runner_secret_field_is_secret() -> None:
    made = settings(runner_secret=SECRET)
    assert isinstance(made.runner_secret, SecretStr)


# An execution run that ends without a result leaves its proposal reconciling


async def _execution(rig: Rig) -> str:
    await _approved(rig.store)
    run = await rig.store.enqueue_run(RunKind.EXECUTION, CH, THREAD, proposal_id="p1")
    return run.id


async def _proposal_state(rig: Rig) -> ProposalState:
    proposal = await rig.store.get_proposal("p1")
    assert proposal is not None
    return proposal.state


@pytest.mark.parametrize("path", ["exit 0", "exit 3", "lost", "runner timeout", "refused", "down"])
async def test_an_execution_run_that_ends_without_a_result_reconciles(rig: Rig, path: str) -> None:
    run_id = await _execution(rig)
    rig.runner.states[run_id] = {
        "exit 0": ["exited:0"],
        "exit 3": ["exited:3"],
        "lost": [404],
        "runner timeout": ["timed_out:-9"],
    }.get(path, ["running"])
    if path == "refused":
        rig.runner.launch_status = 503
    rig.runner.unreachable = path == "down"
    await rig.supervisor.tick()
    await rig.supervisor.tick()
    assert await rig.status(run_id) is not RunStatus.RUNNING
    assert await _proposal_state(rig) is ProposalState.RECONCILING


async def test_an_overdue_execution_run_reconciles(rig: Rig) -> None:
    run_id = await _execution(rig)
    await rig.supervisor.tick()
    assert await _proposal_state(rig) is ProposalState.EXECUTING
    rig.clock.advance(MAX_RUN_TIMEOUT_SECONDS + 1)
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.TIMED_OUT
    assert await _proposal_state(rig) is ProposalState.RECONCILING


async def test_an_execution_run_that_recorded_its_result_stays_applied(rig: Rig) -> None:
    run_id = await _execution(rig)
    rig.runner.states[run_id] = ["running", "exited:0"]
    await rig.supervisor.tick()
    grant = rig.registry.check(rig.runner.tokens[run_id])
    assert grant is not None
    tools = WriteTools(rig.store, POLICY, Poster(), clock=rig.clock)
    await tools.record_result(grant, RecordResultArgs(issue_url=ISSUE_URL))
    await rig.supervisor.tick()
    await rig.supervisor.tick()
    assert await rig.status(run_id) is RunStatus.SUCCEEDED
    assert await _proposal_state(rig) is ProposalState.APPLIED
