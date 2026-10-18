"""The orchestrator `jotflow run` drives: start and stop order, runs on and off (#55)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.adapters.runner import Runner, build_runner_app
from jotflow.commands.run import make_mcp_server
from jotflow.config import Settings
from jotflow.models import MessageRef, ProposalState, RunKind, RunStatus
from jotflow.orchestrator.ingest import Accepted, BotIdentity, Ignored
from jotflow.orchestrator.launch import RunnerClient, RunnerEndpoint
from jotflow.orchestrator.service import Orchestrator, RunKinds, decide_run_kinds
from jotflow.policy import Policy
from jotflow.store import MemoryStore, Store
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolSpec
from tests.test_launch import EXEC_SECRET, Runners
from tests.test_runner import ENVIRON, SECRET, _free_port, config
from tests.test_runs import CH, ISSUE_URL, THREAD, _approved

IDENTITY = BotIdentity(user_id="U0BOT", bot_id="B0BOT")


def policy(*, preset: str | None = "claude-code", debounce: int = 1) -> Policy:
    agent: dict[str, Any] = {"max_concurrent_runs": 2}
    if preset is not None:
        agent["preset"] = preset
    return Policy.model_validate(
        {
            "debounce_seconds": debounce,
            "agent": agent,
            "channels": {
                CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]}
            },
        }
    )


def settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "runner_secret": SECRET,
        "runner_exec_secret": EXEC_SECRET,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


class FakeServer:
    """Records the order of events, and how many tokens are live as it stops."""

    def __init__(
        self, events: list[str], registry: TokenRegistry, tools: Sequence[ToolSpec]
    ) -> None:
        self.events = events
        self.registry = registry
        self.tools = tools
        self.live_at_stop: int | None = None
        self.fail = False

    async def start(self) -> None:
        self.events.append("mcp start")
        if self.fail:
            raise RuntimeError("port in use")

    async def stop(self) -> None:
        self.live_at_stop = self.registry.active()
        self.events.append("mcp stop")


class Rig:
    def __init__(self, *, runs: bool = True, fail_server: bool = False, **overrides: Any) -> None:
        self.events: list[str] = []
        self.store = MemoryStore()
        self.server: FakeServer | None = None
        self.calls: list[tuple[str, str, str]] = []
        self.fail_server = fail_server
        self.runner = RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", SECRET),
            execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET),
            transport=httpx2.MockTransport(self._runner),
        )
        self.orchestrator = Orchestrator(
            settings(**overrides),
            policy(preset="claude-code" if runs else None),
            AsyncWebClient(token="xoxb-test"),
            IDENTITY,
            make_server=self._server,
            open_store=self._open,
            runner=self.runner,
            flush_seconds=0.01,
            tick_seconds=0.01,
        )

    def _runner(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append((request.method, str(request.url.host), request.url.path))
        if request.method == "POST":
            return httpx2.Response(202, json={})
        run_id = request.url.path.rsplit("/", 1)[1]
        state = "killed" if request.method == "DELETE" else "running"
        return httpx2.Response(200, json={"run_id": run_id, "state": state})

    def _server(
        self, _: Settings, registry: TokenRegistry, tools: Sequence[ToolSpec]
    ) -> FakeServer:
        self.server = FakeServer(self.events, registry, tools)
        self.server.fail = self.fail_server
        return self.server

    async def _open(self, path: str) -> Store:
        self.events.append(f"open {path}")
        original = self.store.close

        async def close() -> None:
            self.events.append("store close")
            await original()

        self.store.close = close  # type: ignore[method-assign]
        return self.store


def accepted(ts: str = THREAD) -> Accepted:
    return Accepted(kind="message", ref=MessageRef(channel_id=CH, ts=ts))


async def until(condition: Any, *, tries: int = 500) -> None:
    for _ in range(tries):
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


def test_each_run_kind_needs_a_preset_and_its_own_runner_secret() -> None:
    assert decide_run_kinds(settings(), policy()) == RunKinds(frozenset(RunKind), ())
    none = decide_run_kinds(settings(), policy(preset=None))
    assert none.on == frozenset() and "preset is not set" in none.off[0]
    no_exec = decide_run_kinds(settings(runner_exec_secret=None), policy())
    assert no_exec.on == {RunKind.DRAFTING, RunKind.QUESTION}
    assert no_exec.off == ("execution runs are off: JOTFLOW_RUNNER_EXEC_SECRET is not set",)
    no_draft = decide_run_kinds(settings(runner_secret=None), policy())
    assert no_draft.on == {RunKind.EXECUTION}
    assert no_draft.off == ("drafting and question runs are off: JOTFLOW_RUNNER_SECRET is not set",)


async def test_with_runs_off_the_tools_are_served_and_nothing_is_queued(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(runs=False)
    with caplog.at_level(logging.INFO, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert "agent runs are off: [agent] preset is not set" in caplog.text
    assert not rig.orchestrator.runs_on
    assert rig.server is not None
    assert {t.name for t in rig.server.tools} >= {"get_work", "propose_issue", "record_result"}
    await rig.orchestrator.note(accepted())
    await asyncio.sleep(0.05)
    assert await rig.store.thread_runs(CH, THREAD) == []
    await rig.orchestrator.stop()
    assert rig.events == ["open jotflow.db", "mcp start", "mcp stop", "store close"]
    assert rig.calls == []


async def test_a_quiet_thread_is_queued_and_launched_on_its_runner() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    await rig.orchestrator.note(accepted())
    await rig.orchestrator.note(Ignored(reason="bot message"))  # type: ignore[call-arg]

    async def launched() -> bool:
        return ("POST", "agent", "/runs") in rig.calls

    await until(launched)
    (run,) = await rig.store.thread_runs(CH, THREAD)
    assert run.kind is RunKind.DRAFTING and run.status is RunStatus.RUNNING
    assert rig.orchestrator.registry.active() == 1
    await rig.orchestrator.stop()
    assert ("DELETE", "agent", f"/runs/{run.id}") in rig.calls
    # Every token is gone before the MCP server stops, and the store closes last.
    assert rig.server is not None and rig.server.live_at_stop == 0
    assert rig.events[-2:] == ["mcp stop", "store close"]
    # The run stays running in the store, for the next start's recovery.
    stopped = await rig.store.get_run(run.id)
    assert stopped is not None and stopped.status is RunStatus.RUNNING


async def test_start_recovers_interrupted_runs_first() -> None:
    rig = Rig(runs=False)
    old = await rig.store.enqueue_run(RunKind.DRAFTING, CH, THREAD)
    assert await rig.store.claim_next_run(max_running=2) is not None
    await rig.orchestrator.start()
    recovered = await rig.store.get_run(old.id)
    assert recovered is not None and recovered.status is RunStatus.INTERRUPTED
    await rig.orchestrator.stop()


async def test_a_failed_start_stops_what_had_started() -> None:
    rig = Rig(fail_server=True)
    with pytest.raises(RuntimeError, match="port in use"):
        await rig.orchestrator.start()
    assert rig.events == ["open jotflow.db", "mcp start", "mcp stop", "store close"]
    with pytest.raises(RuntimeError, match="has not started"):
        _ = rig.orchestrator.store
    await rig.orchestrator.stop()  # a second stop is harmless


async def test_a_debouncer_error_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig()
    await rig.orchestrator.start()
    assert rig.orchestrator.store is rig.store

    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("store is down")

    monkeypatch.setattr(rig.store, "get_message", broken)
    monkeypatch.setattr(rig.store, "enqueue_run", broken)
    with caplog.at_level(logging.ERROR, logger="jotflow.orchestrator"):
        reply = Accepted(kind="message", ref=MessageRef(channel_id=CH, ts="2.0", thread_ts="1.0"))
        await rig.orchestrator.note(reply)
        await asyncio.sleep(1.2)
    await rig.orchestrator.stop()
    assert "could not note a message" in caplog.text
    assert "debouncer flush failed" in caplog.text


# End to end in process: the real MCP server and the real exec runner, with a
# stub agent that calls get_work and record_result with its token.

AGENT = r"""
import json, sys, urllib.request
config_path, out = sys.argv[1], sys.argv[2]
server = json.load(open(config_path))["mcpServers"]["jotflow"]
meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "stub", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {}}
calls = [("get_work", {}),
         ("record_result", {"issue_url": "https://github.com/someone-else/web/issues/1"}),
         ("record_result", {"issue_url": sys.argv[3]})]
seen = []
for n, (name, arguments) in enumerate(calls):
    body = json.dumps({"jsonrpc": "2.0", "id": n + 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments, "_meta": meta}}).encode()
    request = urllib.request.Request(server["url"], data=body, method="POST", headers={
        **server["headers"], "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": name})
    with urllib.request.urlopen(request, timeout=10) as reply:
        seen.append(json.loads(reply.read()))
json.dump(seen, open(out, "w"))
"""


async def test_an_execution_run_end_to_end_records_only_the_approved_repo(
    tmp_path: Path,
) -> None:
    port = _free_port()
    script = tmp_path / "stub_agent.py"
    script.write_text(AGENT, encoding="utf-8")
    out = tmp_path / "seen.json"
    exec_cfg = config(
        tmp_path,
        execution=True,
        secret=EXEC_SECRET,
        allowed_hosts=frozenset({"agent-exec:8766"}),
        mcp_url=f"http://127.0.0.1:{port}/mcp",
        command=(sys.executable, str(script), "{mcp_config}", str(out), ISSUE_URL),
    )
    exec_runner = Runner(exec_cfg, environ=ENVIRON)
    client = RunnerClient(
        conversation=None,
        execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET),
        transport=Runners({"agent-exec:8766": build_runner_app(exec_runner, exec_cfg)}),
    )
    store = MemoryStore()
    await _approved(store)

    async def opened(_: str) -> Store:
        return store

    orchestrator = Orchestrator(
        settings(mcp_port=port),
        policy(),
        AsyncWebClient(token="xoxb-test"),
        IDENTITY,
        make_server=make_mcp_server,
        open_store=opened,
        runner=client,
        tick_seconds=0.02,
    )
    # Startup recovery queues one execution run for the approved proposal.
    await orchestrator.start()
    try:

        async def done() -> bool:
            runs = await store.thread_runs(CH, THREAD)
            return any(r.status is RunStatus.SUCCEEDED for r in runs)

        await until(done, tries=1000)
    finally:
        await orchestrator.stop()
        await exec_runner.close()
    (run,) = await store.thread_runs(CH, THREAD)
    assert run.kind is RunKind.EXECUTION
    get_work, wrong, right = json.loads(out.read_text(encoding="utf-8"))
    work = get_work["result"]["structuredContent"]
    assert work["kind"] == "execution" and work["execution"]["proposal"]["repo"] == "acme-co/web"
    assert wrong["result"]["isError"] is True
    assert "does not match the approved repo" in wrong["result"]["content"][0]["text"]
    assert right["result"]["structuredContent"]["state"] == ProposalState.APPLIED
    proposal = await store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.APPLIED
    assert proposal.result is not None and proposal.result.issue_url == ISSUE_URL
    assert orchestrator.registry.active() == 0


async def test_the_run_token_never_reaches_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    await rig.orchestrator.start()
    tokens: list[str] = []
    original = rig.orchestrator.registry.issue

    def capture(grant: Grant, ttl: float) -> str:
        token = original(grant, ttl)
        tokens.append(token)
        return token

    rig.orchestrator.registry.issue = capture  # type: ignore[method-assign]
    await rig.store.enqueue_run(RunKind.DRAFTING, CH, THREAD)

    async def launched() -> bool:
        return bool(tokens) and ("POST", "agent", "/runs") in rig.calls

    await until(launched)
    await rig.orchestrator.stop()
    assert tokens and tokens[0] not in caplog.text
    assert SECRET not in caplog.text and EXEC_SECRET not in caplog.text


async def test_without_the_exec_secret_execution_runs_stay_queued_and_approved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(runner_exec_secret=None)
    await _approved(rig.store)
    with caplog.at_level(logging.INFO, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert "execution runs are off: JOTFLOW_RUNNER_EXEC_SECRET is not set" in caplog.text
    assert "agent runs are on for drafting, question" in caplog.text
    await rig.orchestrator.note(accepted("1760781700.000100"))

    async def drafted() -> bool:
        return ("POST", "agent", "/runs") in rig.calls

    await until(drafted)
    await asyncio.sleep(0.05)
    (execution,) = [
        r for r in await rig.store.thread_runs(CH, THREAD) if r.kind is RunKind.EXECUTION
    ]
    assert execution.status is RunStatus.QUEUED
    proposal = await rig.store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.APPROVED
    assert all(host != "agent-exec" for _, host, _ in rig.calls)
    await rig.orchestrator.stop()


async def test_without_the_drafting_secret_nothing_is_queued_but_execution_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(runner_secret=None)
    await _approved(rig.store)
    with caplog.at_level(logging.INFO, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert "drafting and question runs are off: JOTFLOW_RUNNER_SECRET is not set" in caplog.text

    async def executed() -> bool:
        return ("POST", "agent-exec", "/runs") in rig.calls

    await until(executed)
    await rig.orchestrator.note(accepted("1760781700.000100"))
    await asyncio.sleep(0.05)
    assert await rig.store.thread_runs(CH, "1760781700.000100") == []
    assert all(host != "agent" for _, host, _ in rig.calls)
    await rig.orchestrator.stop()
