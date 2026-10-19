"""The orchestrator `jotflow run` drives: start and stop order, runs on and off (#55)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.adapters.runner import Runner, build_runner_app
from jotflow.commands.operator import new_token
from jotflow.commands.run import make_mcp_server
from jotflow.config import Settings
from jotflow.models import RUN_KINDS, MessageRef, ProposalState, RunKind, RunStatus
from jotflow.orchestrator.ingest import Accepted, BotIdentity, Ignored
from jotflow.orchestrator.launch import RunnerClient, RunnerEndpoint
from jotflow.orchestrator.service import Orchestrator, RunKinds, decide_run_kinds
from jotflow.policy import Policy
from jotflow.store import MemoryStore, Store
from jotflow.whiteboard.operator_tokens import GateTokens
from jotflow.whiteboard.tokens import Grant, TokenCheck, TokenRegistry
from jotflow.whiteboard.tools import ToolSpec
from tests.test_card_sync import FakeCardPoster, FakePeople
from tests.test_help import FakeHelpSlack
from tests.test_keyword_handler import FakeSlack
from tests.test_launch import EXEC_SECRET, Runners
from tests.test_runner import ENVIRON, SECRET, _free_port, config
from tests.test_runs import CH, ISSUE_URL, THREAD, _approved
from tests.test_thread_notice import FakeThreadSlack

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
        self,
        events: list[str],
        registry: TokenRegistry,
        tokens: TokenCheck,
        tools: Sequence[ToolSpec],
    ) -> None:
        self.events = events
        self.registry = registry
        self.tokens = tokens
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
    def __init__(
        self,
        *,
        runs: bool = True,
        fail_server: bool = False,
        preset: str = "claude-code",
        **overrides: Any,
    ) -> None:
        self.events: list[str] = []
        self.store = MemoryStore()
        self.server: FakeServer | None = None
        self.calls: list[tuple[str, str, str]] = []
        self.fail_server = fail_server
        self.info_preset = "claude-code"
        self.exec_preset: str | None = None  # None: the exec runner starts info_preset too
        self.info_status = 200
        self.slack = FakeSlack()
        self.help = FakeHelpSlack()
        self.cards = FakeCardPoster()
        self.threads = FakeThreadSlack()
        self.runner = RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", SECRET),
            execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET),
            transport=httpx2.MockTransport(self._runner),
        )
        self.orchestrator = Orchestrator(
            settings(**overrides),
            policy(preset=preset if runs else None),
            AsyncWebClient(token="xoxb-test"),
            IDENTITY,
            make_server=self._server,
            open_store=self._open,
            runner=self.runner,
            keyword_slack=self.slack,
            help_slack=self.help,
            card_poster=self.cards,
            thread_slack=self.threads,
            people=FakePeople(),
            card_seconds=0.01,
            flush_seconds=0.01,
            tick_seconds=0.01,
            info_attempts=2,
            info_delay=0.0,
        )

    def _runner(self, request: httpx2.Request) -> httpx2.Response:
        host = str(request.url.host)
        self.calls.append((request.method, host, request.url.path))
        if request.url.path == "/info":
            if self.info_status == 0:
                raise httpx2.ConnectError("down")
            if self.info_status != 200:
                return httpx2.Response(self.info_status, json={})
            kinds = ["execution"] if host == "agent-exec" else ["drafting", "question"]
            started = self.info_preset
            if host == "agent-exec" and self.exec_preset is not None:
                started = self.exec_preset
            return httpx2.Response(200, json={"preset": started, "kinds": kinds})
        if request.method == "POST":
            return httpx2.Response(202, json={})
        run_id = request.url.path.rsplit("/", 1)[1]
        state = "killed" if request.method == "DELETE" else "running"
        return httpx2.Response(200, json={"run_id": run_id, "state": state})

    def _server(self, _: Settings, tokens: TokenCheck, tools: Sequence[ToolSpec]) -> FakeServer:
        self.server = FakeServer(self.events, self.orchestrator.registry, tokens, tools)
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
    assert decide_run_kinds(settings(), policy()) == RunKinds(RUN_KINDS, ())
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

    # Arm a thread while the store works, so the flush below has one to queue.
    await rig.orchestrator.note(accepted("1760781900.000100"))
    monkeypatch.setattr(rig.store, "get_message", broken)
    monkeypatch.setattr(rig.store, "queue_pending", broken)
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
        policy(preset="command"),
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


# Each runner is asked what it starts, with its own secret, before runs begin.


async def test_runners_that_match_jotflow_toml_turn_their_kinds_on() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    assert rig.orchestrator.kinds.on == RUN_KINDS
    assert ("GET", "agent", "/info") in rig.calls and ("GET", "agent-exec", "/info") in rig.calls
    await rig.orchestrator.stop()


async def test_a_runner_with_another_preset_turns_its_kinds_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.info_preset = "codex"
    await _approved(rig.store)
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert not rig.orchestrator.runs_on
    assert (
        "execution runs are off: the runner at http://agent-exec:8766 starts preset codex, "
        "but jotflow.toml's [agent] preset is claude-code"
    ) in caplog.text
    assert "drafting and question runs are off" in caplog.text
    await asyncio.sleep(0.05)
    assert not [c for c in rig.calls if c[0] == "POST"]
    proposal = await rig.store.get_proposal("p1")
    assert proposal is not None and proposal.state is ProposalState.APPROVED
    await rig.orchestrator.stop()


async def test_a_codex_policy_hands_execution_to_a_claude_code_runner() -> None:
    # The documented codex setup (#172): agent.env runs codex, agent-exec.env claude-code.
    rig = Rig(preset="codex")
    rig.info_preset = "codex"
    rig.exec_preset = "claude-code"
    await rig.orchestrator.start()
    assert rig.orchestrator.kinds.on == RUN_KINDS
    await rig.orchestrator.stop()


async def test_a_codex_policy_refuses_a_codex_execution_runner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(preset="codex")
    rig.info_preset = "codex"
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert rig.orchestrator.kinds.on == RUN_KINDS - {RunKind.EXECUTION}
    assert (
        "execution runs are off: the runner at http://agent-exec:8766 starts preset codex, "
        "but jotflow.toml's [agent] preset codex does not serve execution runs; "
        "that runner must start claude-code"
    ) in caplog.text
    await rig.orchestrator.stop()


async def test_a_claude_code_policy_refuses_a_codex_execution_runner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.exec_preset = "codex"
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert rig.orchestrator.kinds.on == RUN_KINDS - {RunKind.EXECUTION}
    assert (
        "execution runs are off: the runner at http://agent-exec:8766 starts preset codex, "
        "but jotflow.toml's [agent] preset is claude-code"
    ) in caplog.text
    await rig.orchestrator.stop()


async def test_a_command_policy_never_hands_execution_to_claude_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Only a known preset that leaves out execution gets the claude-code exception.
    rig = Rig(preset="command")
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert RunKind.EXECUTION not in rig.orchestrator.kinds.on
    assert (
        "execution runs are off: the runner at http://agent-exec:8766 starts preset "
        "claude-code, but jotflow.toml's [agent] preset is command"
    ) in caplog.text
    await rig.orchestrator.stop()


async def test_a_codex_policy_still_needs_codex_for_drafting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(preset="codex")
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert rig.orchestrator.kinds.on == frozenset({RunKind.EXECUTION})
    assert (
        "the runner at http://agent:8766 starts preset claude-code, "
        "but jotflow.toml's [agent] preset is codex"
    ) in caplog.text
    await rig.orchestrator.stop()


async def test_a_wrong_secret_is_not_retried_and_turns_the_kind_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.info_status = 401
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert not rig.orchestrator.runs_on
    assert "did not answer: the runner refused JotFlow's secret (401)" in caplog.text
    assert sum(1 for c in rig.calls if c == ("GET", "agent", "/info")) == 1
    await rig.orchestrator.stop()


async def test_an_unreachable_runner_is_retried_then_its_kinds_are_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.info_status = 0
    with caplog.at_level(logging.WARNING, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()
    assert sum(1 for c in rig.calls if c == ("GET", "agent-exec", "/info")) == 2
    assert (
        "execution runs are off: the runner at http://agent-exec:8766 did not answer" in caplog.text
    )
    await rig.orchestrator.stop()


async def test_a_runner_serving_other_kinds_turns_them_off() -> None:
    from jotflow.orchestrator.launch import RunnerInfo
    from jotflow.orchestrator.service import RunKinds, check_runners

    class Client:
        def endpoint_for(self, kind: RunKind) -> RunnerEndpoint:
            return RunnerEndpoint("http://agent:8766", SECRET)

        async def info(self, kind: RunKind) -> RunnerInfo:
            return RunnerInfo(preset="claude-code", kinds=(RunKind.DRAFTING,))

    checked = await check_runners(
        Client(),  # type: ignore[arg-type]
        RunKinds(frozenset({RunKind.DRAFTING, RunKind.QUESTION}), ()),
        "claude-code",
        attempts=1,
    )
    assert checked.on == frozenset()
    assert checked.off == (
        "drafting and question runs are off: the runner at http://agent:8766 serves drafting",
    )


# Keywords before the debouncer, through the real Bolt app (#56)

REPLY = "1760781660.000100"


async def _authorize(**_: Any) -> Any:
    from slack_bolt.authorization import AuthorizeResult

    return AuthorizeResult(
        enterprise_id=None,
        team_id="T0ACME",
        bot_user_id=IDENTITY.user_id,
        bot_id=IDENTITY.bot_id,
        bot_token="xoxb-test",
    )


async def _bolt_app(rig: Rig) -> Any:
    from slack_bolt.async_app import AsyncApp

    from jotflow.orchestrator.ingest import EventFilter
    from jotflow.orchestrator.slack import register_handlers

    app = AsyncApp(
        client=AsyncWebClient(token="xoxb-test"),
        authorize=_authorize,
        request_verification_enabled=False,
        process_before_response=True,
    )
    register_handlers(app, EventFilter(policy().channels, IDENTITY), rig.orchestrator.note)
    return app


async def _deliver(app: Any, event: dict[str, Any], event_id: str) -> None:
    from slack_bolt.request.async_request import AsyncBoltRequest

    body = {"type": "event_callback", "team_id": "T0ACME", "event_id": event_id, "event": event}
    request = AsyncBoltRequest(
        body=json.dumps(body), headers={"content-type": ["application/json"]}
    )
    response = await app.async_dispatch(request)
    assert response.status == 200


def _reply(ts: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "channel": CH,
        "ts": ts,
        "thread_ts": THREAD,
        "user": "U0ALEX",
        "text": text,
    }


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_skip_reply_never_reaches_the_debouncers_topic_join() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        topic = await rig.store.create_topic(CH, "Dark mode")
        await rig.store.link_message(MessageRef(channel_id=CH, ts=THREAD), topic.topic.id)
        agent_links: list[str] = []
        original = rig.store.link_message

        async def recording(ref: MessageRef, topic_id: str, *, by: Any = "agent") -> Any:
            if by == "agent":
                agent_links.append(ref.ts)
            return await original(ref, topic_id, by=by)

        rig.store.link_message = recording  # type: ignore[method-assign]
        app = await _bolt_app(rig)
        await _deliver(app, _reply(REPLY, "skip: just a joke"), "Ev1")
        await _deliver(app, _reply("1760781720.000100", "and dark icons too"), "Ev2")
    finally:
        await rig.orchestrator.stop()
    skipped = await rig.store.get_message(CH, REPLY)
    assert skipped is not None and skipped.chatter and skipped.topic_id is None
    assert skipped.linked_by == "person"
    # The control: an ordinary reply in the same thread is joined, by the agent.
    joined = await rig.store.get_message(CH, "1760781720.000100")
    assert joined is not None and joined.topic_id == topic.topic.id and joined.linked_by == "agent"
    assert rig.slack.reactions == [(CH, REPLY, "white_check_mark")]
    # Never joined at all, not joined and then overridden.
    assert agent_links == ["1760781720.000100"]


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_skip_reaction_reaches_the_handler_through_the_app() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        app = await _bolt_app(rig)
        reaction = {
            "type": "reaction_added",
            "user": "U0ALEX",
            "item_user": "U0ALEX",
            "reaction": "no_entry_sign",
            "item": {"type": "message", "channel": CH, "ts": THREAD},
        }
        await _deliver(app, reaction, "Ev3")
    finally:
        await rig.orchestrator.stop()
    link = await rig.store.get_message(CH, THREAD)
    assert link is not None and link.chatter and link.linked_by == "person"


async def test_keywords_apply_even_with_runs_off() -> None:
    rig = Rig(runs=False)
    await rig.orchestrator.start()
    try:
        event = {"type": "message", "channel": CH, "ts": THREAD, "text": "skip: testing"}
        await rig.orchestrator.note(accepted(), event)
    finally:
        await rig.orchestrator.stop()
    link = await rig.store.get_message(CH, THREAD)
    assert link is not None and link.chatter


async def test_a_keyword_error_is_logged_and_the_debouncer_still_runs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:

        async def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("store is down")

        monkeypatch.setattr(rig.store, "mark_chatter", broken)
        event = {"type": "message", "channel": CH, "ts": THREAD, "text": "skip: testing"}
        with caplog.at_level(logging.ERROR, logger="jotflow.orchestrator"):
            await rig.orchestrator.note(accepted(), event)
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending()
    finally:
        await rig.orchestrator.stop()
    assert "could not apply a keyword" in caplog.text


async def _until_flushed(rig: Rig) -> None:
    await asyncio.sleep(1.3)  # past the one-second debounce, with flushes every 10 ms


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_skip_message_queues_no_drafting_run() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        app = await _bolt_app(rig)
        root = {"type": "message", "channel": CH, "ts": THREAD, "user": "U0ALEX"}
        await _deliver(app, {**root, "text": "skip: just testing the bot"}, "Ev4")
        await _deliver(app, _reply(REPLY, "it did not react, nice"), "Ev7")
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending() == {}
        await _until_flushed(rig)
        runs = await rig.store.thread_runs(CH, THREAD)
    finally:
        await rig.orchestrator.stop()
    link = await rig.store.get_message(CH, THREAD)
    assert link is not None and link.chatter and link.linked_by == "person"
    assert runs == []
    assert not [call for call in rig.calls if call[0] == "POST"]


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_skip_reply_leaves_the_threads_wait_alone() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        app = await _bolt_app(rig)
        await _deliver(app, _reply(REPLY, "skip: off topic"), "Ev5")
        assert rig.orchestrator._debouncer is not None
        assert not rig.orchestrator._debouncer.pending()
    finally:
        await rig.orchestrator.stop()


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_an_re_reply_joins_the_named_issues_topic_through_the_app() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        parent = await rig.store.create_topic(CH, "Dark mode")
        await rig.store.link_message(MessageRef(channel_id=CH, ts=THREAD), parent.topic.id)
        export = await rig.store.create_topic(CH, "Export as CSV")
        await rig.store.set_topic_issue(export.topic.id, "acme-co/web", 42)
        agent_links: list[str] = []
        original = rig.store.link_message

        async def recording(ref: MessageRef, topic_id: str, *, by: Any = "agent") -> Any:
            if by == "agent":
                agent_links.append(ref.ts)
            return await original(ref, topic_id, by=by)

        rig.store.link_message = recording  # type: ignore[method-assign]
        app = await _bolt_app(rig)
        await _deliver(app, _reply(REPLY, "re: web#42 same thing"), "Ev6")
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending()  # the thread still drafts, now with #42
    finally:
        await rig.orchestrator.stop()
    link = await rig.store.get_message(CH, REPLY)
    assert link is not None and link.linked_by == "person"
    assert link.topic_id == export.topic.id != parent.topic.id
    assert agent_links == []  # the debouncer never joined it to the parent's topic
    assert rig.slack.reactions == [(CH, REPLY, "white_check_mark")]


async def test_a_thread_waiting_at_shutdown_is_drafted_after_a_restart() -> None:
    from jotflow.store.base import utc_now

    rig = Rig()
    await rig.store.arm_thread(CH, THREAD, utc_now())
    await rig.store.arm_thread(CH, "1760781700.000100", utc_now())
    await rig.store.mark_chatter(MessageRef(channel_id=CH, ts="1760781700.000100"), by="person")
    await rig.orchestrator.start()

    async def launched() -> bool:
        return ("POST", "agent", "/runs") in rig.calls

    await until(launched)
    (run,) = await rig.store.thread_runs(CH, THREAD)
    assert run.kind is RunKind.DRAFTING
    assert await rig.store.thread_runs(CH, "1760781700.000100") == []
    assert await rig.store.pending_threads() == []
    await rig.orchestrator.stop()


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_skip_reaction_inside_the_window_means_no_drafting_run() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        app = await _bolt_app(rig)
        post = {"type": "message", "channel": CH, "ts": THREAD, "text": "maybe a dark mode?"}
        await rig.orchestrator.note(accepted(), post)
        assert await rig.store.pending_threads()
        reaction = {
            "type": "reaction_added",
            "user": "U0ALEX",
            "item_user": "U0ALEX",
            "reaction": "no_entry_sign",
            "item": {"type": "message", "channel": CH, "ts": THREAD},
        }
        await _deliver(app, reaction, "Ev9")
        await asyncio.sleep(1.3)  # past the 1 s window, with the flush loop running
    finally:
        await rig.orchestrator.stop()
    assert await rig.store.thread_runs(CH, THREAD) == []
    assert await rig.store.pending_threads() == []
    assert not [c for c in rig.calls if c[0] == "POST"]


# Help in the channel, after keywords (#80)


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_first_message_through_the_app_gets_the_guide_and_a_tip() -> None:
    from jotflow.orchestrator.help import TIP, guide_text, nudge

    rig = Rig()
    await rig.orchestrator.start()
    try:
        app = await _bolt_app(rig)
        root = {"type": "message", "channel": CH, "ts": THREAD, "user": "U0ALEX"}
        await _deliver(app, {**root, "text": "Dark mode would be nice"}, "Ev8")
        await _deliver(app, _reply(REPLY, "skip"), "Ev9")
    finally:
        await rig.orchestrator.stop()
    assert rig.help.guides == [(CH, guide_text())]
    # One notice per message: the tip first, then the near miss gets its one nudge, in-thread.
    assert rig.help.whispers == [
        (CH, "U0ALEX", None, TIP),
        (CH, "U0ALEX", THREAD, nudge("skip")),
    ]


async def test_a_paused_channel_gets_no_help() -> None:
    from jotflow.models import ChannelMode

    rig = Rig()
    await rig.orchestrator.start()
    try:
        await rig.store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
        event = {"type": "message", "channel": CH, "ts": THREAD, "user": "U0ALEX", "text": "hi"}
        await rig.orchestrator.note(accepted(), event)
        await rig.orchestrator.ensure_guides([CH])
    finally:
        await rig.orchestrator.stop()
    assert rig.help.whispers == rig.help.guides == []


async def test_guides_are_pinned_at_startup_once() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        await rig.orchestrator.ensure_guides([CH])
        await rig.orchestrator.ensure_guides([CH])
        assert await rig.orchestrator.channel_mode(CH) is not None
    finally:
        await rig.orchestrator.stop()
    assert len(rig.help.guides) == 1


async def test_a_help_failure_is_logged_and_the_debouncer_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()

    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("slack is down")

    rig.help.whisper = broken  # type: ignore[method-assign]
    rig.help.post_guide = broken  # type: ignore[method-assign]
    await rig.orchestrator.start()
    try:
        event = {"type": "message", "channel": CH, "ts": THREAD, "user": "U0ALEX", "text": "hi"}
        with caplog.at_level(logging.ERROR, logger="jotflow.orchestrator"):
            await rig.orchestrator.note(accepted(), event)
            await rig.orchestrator.ensure_guides(["C0OTHER01"])
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending()
    finally:
        await rig.orchestrator.stop()
    assert "could not offer help" in caplog.text


# The live card (#152)


async def test_a_proposal_gets_its_card_from_the_running_service() -> None:
    from tests.test_card_sync import approve, proposal

    rig = Rig()
    await rig.orchestrator.start()
    try:
        await rig.store.add_proposal(proposal())

        async def posted() -> bool:
            return len(rig.cards.posts) == 1

        await until(posted)
        await approve(rig.store)

        async def edited() -> bool:
            return len(rig.cards.updates) == 1

        await until(edited)
    finally:
        await rig.orchestrator.stop()
    record = await rig.store.get_proposal("p1")
    assert record is not None and record.card_ts == "1760782000.000001"
    assert rig.cards.updates[0][1] == record.card_ts
    assert len(rig.cards.posts) == 1


async def test_cards_are_drawn_even_while_agent_runs_are_off() -> None:
    from tests.test_card_sync import proposal

    rig = Rig(runs=False)
    await rig.orchestrator.start()
    try:
        await rig.store.add_proposal(proposal())

        async def posted() -> bool:
            return len(rig.cards.posts) == 1

        await until(posted)
        assert rig.orchestrator.cards is not None
        assert not await rig.orchestrator.cards.refresh("p1")
    finally:
        await rig.orchestrator.stop()


async def test_a_card_sync_error_is_logged_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig()
    calls = 0

    async def broken(*_: Any, **__: Any) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("store is down")

    monkeypatch.setattr(rig.store, "cards_due", broken)
    with caplog.at_level(logging.ERROR, logger="jotflow.orchestrator"):
        await rig.orchestrator.start()

        async def twice() -> bool:
            return calls >= 2

        await until(twice)
        await rig.orchestrator.stop()
    assert "card sync failed" in caplog.text


async def test_a_drafting_runs_propose_issue_yields_one_card() -> None:
    # Runs off, so no supervisor takes the run: this test plays the drafting agent.
    rig = Rig(runs=False)
    await rig.orchestrator.start()
    try:
        assert rig.server is not None
        specs = {spec.name: spec for spec in rig.server.tools}
        await rig.store.enqueue_run(RunKind.DRAFTING, CH, THREAD)
        run = await rig.store.claim_next_run(max_running=1)
        assert run is not None
        grant = Grant(run_id=run.id, kind=RunKind.DRAFTING, channel_id=CH, thread_ts=THREAD)
        packet = [MessageRef(channel_id=CH, ts=THREAD, thread_ts=THREAD)]
        await rig.store.record_packet(run.id, packet)

        async def call(tool: str, arguments: dict[str, Any]) -> Any:
            spec = specs[tool]
            return await spec.handler(grant, spec.input_model.model_validate(arguments))

        linked = await call(
            "link_message",
            {"ts": THREAD, "decision": "new_topic", "title": "Dark mode", "confidence": 0.9},
        )
        await call(
            "propose_issue",
            {
                "topic_id": linked.topic_id,
                "repo": "acme-co/web",
                "title": "Add a dark mode",
                "body": "People asked for a dark theme.",
                "sources": [{"channel_id": CH, "ts": THREAD}],
                "confidence": 0.8,
            },
        )
        assert rig.cards.posts == []  # the tool write itself never calls Slack

        async def posted() -> bool:
            return len(rig.cards.posts) == 1

        await until(posted)
    finally:
        await rig.orchestrator.stop()
    ((channel, thread, card),) = rig.cards.posts
    assert (channel, thread) == (CH, THREAD)
    assert "Add a dark mode" in json.dumps(card.blocks)
    (stored,) = await rig.store.thread_proposals(CH, THREAD)
    assert stored.card_ts is not None and not stored.card_due


async def test_the_approval_gate_is_built_and_arms_through_the_debouncer() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        assert rig.orchestrator.approval is not None
        await rig.orchestrator._arm_thread(CH, "1760781800.000100")
        assert [p.thread_ts for p in await rig.store.pending_threads()] == ["1760781800.000100"]
    finally:
        await rig.orchestrator.stop()
    off = Rig(runs=False)
    await off.orchestrator.start()
    try:
        assert off.orchestrator.approval is not None
        await off.orchestrator._arm_thread(CH, "1760781800.000100")  # no debouncer: no-op
        assert await off.store.pending_threads() == []
    finally:
        await off.orchestrator.stop()


async def test_a_reaction_reaches_the_approval_gate_after_keywords(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        assert rig.orchestrator.approval is not None
        seen: list[Any] = []

        async def recorded(event: Any) -> str:
            seen.append(event)
            if len(seen) == 2:
                raise RuntimeError("slack is down")
            return "ignored"

        monkeypatch.setattr(rig.orchestrator.approval, "on_reaction", recorded)
        event = {
            "type": "reaction_added",
            "user": "U0ALEX",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": CH, "ts": THREAD},
        }
        reaction = Accepted("reaction", MessageRef(channel_id=CH, ts=THREAD), reaction="x")
        await rig.orchestrator.note(reaction, event)
        with caplog.at_level(logging.ERROR, logger="jotflow.orchestrator"):
            await rig.orchestrator.note(reaction, event)
        await rig.orchestrator.note(accepted(), {"type": "message", "channel": CH, "ts": THREAD})
    finally:
        await rig.orchestrator.stop()
    assert seen == [event, event]
    assert "could not handle a reaction on a card" in caplog.text


# Channel modes (#28)


async def test_a_paused_channel_reads_nothing() -> None:
    from jotflow.models import ChannelMode

    rig = Rig()
    await rig.orchestrator.start()
    try:
        await rig.store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
        event = {
            "type": "message",
            "channel": CH,
            "ts": THREAD,
            "user": "U0ALEX",
            "text": "skip: x",
        }
        await rig.orchestrator.note(accepted(), event)
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending() == {}
    finally:
        await rig.orchestrator.stop()
    assert await rig.store.get_message(CH, THREAD) is None  # the keyword was not read
    assert rig.slack.reactions == [] and rig.help.whispers == []


async def test_an_unreadable_mode_fails_closed(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:

        async def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("store is down")

        monkeypatch.setattr(rig.store, "get_channel_mode", broken)
        event = {"type": "message", "channel": CH, "ts": THREAD, "user": "U0ALEX", "text": "hi"}
        with caplog.at_level(logging.ERROR, logger="jotflow.orchestrator"):
            await rig.orchestrator.note(accepted(), event)
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending() == {}
    finally:
        monkeypatch.undo()
        await rig.orchestrator.stop()
    assert "could not read the channel's mode" in caplog.text


async def test_auto_mode_approves_writes_and_posts_the_card_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jotflow.models import ChannelMode, ExecutionResult
    from tests.test_card_sync import proposal

    rig = Rig()
    await rig.orchestrator.start()
    try:
        await rig.store.set_channel_mode(CH, ChannelMode.AUTO, changed_by="U0LEAD")
        await rig.store.add_proposal(proposal())

        async def launched() -> bool:
            return ("POST", "agent-exec", "/runs") in rig.calls

        await until(launched)
        record = await rig.store.get_proposal("p1")
        assert record is not None and record.approved_by == "auto"
        assert record.state is ProposalState.EXECUTING
        await asyncio.sleep(0.05)
        assert rig.cards.posts == []  # no card while the write is under way
        url = "https://github.com/acme-co/web/issues/7"
        await rig.store.transition(
            "p1", ProposalState.APPLIED, result=ExecutionResult(issue_url=url)
        )

        async def posted() -> bool:
            return len(rig.cards.posts) == 1

        await until(posted)
    finally:
        await rig.orchestrator.stop()
    card = json.dumps(rig.cards.posts[0][2].blocks)
    assert "Approved by auto mode" in card and "acme-co/web#7" in card


# The issue link in the thread (#32)


async def test_an_applied_issue_is_told_in_its_thread_by_the_running_service() -> None:
    from tests.test_thread_notice import URL, applied

    rig = Rig(runs=False)
    await rig.orchestrator.start()
    try:
        await applied(rig.store)

        async def told() -> bool:
            return len(rig.threads.replies) == 1

        await until(told)
        await asyncio.sleep(0.05)
    finally:
        await rig.orchestrator.stop()
    assert rig.threads.replies == [(CH, THREAD, f"Created <{URL}|web#7>")]
    assert {name for _, _, name in rig.threads.reactions} == {"ticket"}


# Taking a skip reaction back (#138)


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_removing_a_skip_reaction_lets_the_thread_draft_again() -> None:
    rig = Rig()
    await rig.orchestrator.start()
    try:
        app = await _bolt_app(rig)
        item = {"type": "message", "channel": CH, "ts": THREAD}
        skip = {
            "type": "reaction_added",
            "user": "U0ALEX",
            "item_user": "U0ALEX",
            "reaction": "no_entry_sign",
            "item": item,
        }
        await _deliver(app, skip, "Ev20")
        link = await rig.store.get_message(CH, THREAD)
        assert link is not None and link.chatter
        assert rig.orchestrator._debouncer is not None
        assert rig.orchestrator._debouncer.pending() == {}
        await _deliver(app, {**skip, "type": "reaction_removed"}, "Ev21")
        assert await rig.store.get_message(CH, THREAD) is None
        assert rig.orchestrator._debouncer.pending()  # armed again: it will draft
    finally:
        await rig.orchestrator.stop()


async def test_operator_tokens_reach_the_gate_and_stop_with_the_server() -> None:
    rig = Rig(runs=False)
    await rig.orchestrator.start()
    assert rig.server is not None
    token, made = new_token("alex", [CH], 30, policy(), datetime.now(UTC))
    await rig.store.add_operator_token(made)
    gate = rig.server.tokens
    assert gate.check(token) is None  # not loaded yet: a miss reload lets it in
    assert isinstance(gate, GateTokens)
    grant = await gate.check_on_miss(token)
    assert grant is not None and grant.kind is RunKind.OPERATOR
    await rig.orchestrator.stop()
    assert gate.check(token) is None
    [saved] = await rig.store.operator_tokens()
    assert saved.last_used_at is not None
