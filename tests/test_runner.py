"""The agent container's runner (#54): what a launched run sees, and what it never sees."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow import cli
from jotflow.adapters.runner import (
    DIR_PREFIX,
    ConfigError,
    LaunchRequest,
    Runner,
    RunnerConfig,
    RunState,
    argv_for,
    build_runner_app,
    child_env,
    mcp_config,
    parse_command,
    remove_stale,
)
from jotflow.commands import runner as runner_command
from jotflow.models import RunKind
from jotflow.whiteboard.server import McpServer, build_app
from jotflow.whiteboard.tokens import Grant, TokenRegistry

SECRET = "s" * 40
READ, WRITE = "read-token-value", "write-token-value"
TOKEN = "jfr_" + "t" * 43
HOST = "agent:8766"

# The agent stand-in: records what it was given, then exits, sleeps, or calls MCP.
AGENT = r"""
import json, os, stat, sys, time, urllib.request
config_path, out, mode = sys.argv[1], sys.argv[2], sys.argv[3]
run_dir = os.path.dirname(config_path)
record = {
    "argv": sys.argv[1:],
    "env": dict(os.environ),
    "config_mode": stat.S_IMODE(os.stat(config_path).st_mode),
    "dir_mode": stat.S_IMODE(os.stat(run_dir).st_mode),
    "config": json.load(open(config_path)),
    "cwd": os.getcwd(),
    "run_dir": run_dir,
}
if mode == "mcp":
    server = record["config"]["mcpServers"]["jotflow"]
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "stub", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                       "params": {"_meta": meta}}).encode()
    request = urllib.request.Request(server["url"], data=body, method="POST", headers={
        **server["headers"], "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"})
    with urllib.request.urlopen(request, timeout=10) as reply:
        record["mcp_status"] = reply.status
        record["mcp_result"] = json.loads(reply.read())["result"]
if mode == "sleep":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    record["grandchild"] = child.pid
with open(out, "w") as handle:
    json.dump(record, handle)
if mode == "sleep":
    time.sleep(60)
sys.exit(3 if mode == "fail" else 0)
"""

ENVIRON = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/home/agent",
    "LANG": "C.UTF-8",
    "ANTHROPIC_API_KEY": "model-key",
    "UNLISTED": "not for the agent",
    # Things that must never reach an agent, even if they leaked into the runner.
    "JOTFLOW_SLACK_BOT_TOKEN": "xoxb-should-never-appear",
    "JOTFLOW_SLACK_APP_TOKEN": "xapp-should-never-appear",
    "SLACK_BOT_TOKEN": "xoxb-should-never-appear-either",
    "JOTFLOW_RUNNER_SECRET": SECRET,
    "JOTFLOW_GITHUB_READ_TOKEN": READ,
    "JOTFLOW_GITHUB_WRITE_TOKEN": WRITE,
}


def config(tmp_path: Path, mode: str = "exit", **overrides: Any) -> RunnerConfig:
    script = tmp_path / "agent.py"
    script.write_text(AGENT, encoding="utf-8")
    fields: dict[str, Any] = {
        "secret": SECRET,
        "mcp_url": "http://jotflow:8765/mcp",
        "command": (sys.executable, str(script), "{mcp_config}", str(tmp_path / "out.json"), mode),
        "read_token": READ,
        "write_token": WRITE,
        "env_allow": ("ANTHROPIC_API_KEY",),
        "allowed_hosts": frozenset({HOST}),
        "workdir": tmp_path,
    }
    fields.update(overrides)
    return RunnerConfig(**fields)


def recorded(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))


async def finish(runner: Runner, run_id: str) -> dict[str, Any]:
    for _ in range(500):
        view = runner.get(run_id)
        assert view is not None
        if view["state"] not in ("starting", "running"):
            return view
        await asyncio.sleep(0.02)
    raise AssertionError("the run did not end")


# Configuration


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"secret": "short"}, "at least 32 characters"),
        ({"command": ()}, "agent command is empty"),
        ({"mcp_url": "file:///x"}, "http or https"),
        ({"env_allow": ("bad name",)}, "not an environment variable name"),
        ({"env_allow": ("JOTFLOW_SLACK_BOT_TOKEN",)}, "may not be passed to the agent"),
        ({"env_allow": ("SLACK_SIGNING_SECRET",)}, "may not be passed to the agent"),
        ({"env_allow": ("GITHUB_TOKEN",)}, "may not be passed to the agent"),
        ({"env_allow": ("NPM_TOKEN",)}, "may not be passed to the agent"),
    ],
)
def test_bad_configuration_fails_at_startup(
    tmp_path: Path, overrides: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ConfigError, match=problem):
        config(tmp_path, **overrides)


def test_the_config_never_shows_its_secrets(tmp_path: Path) -> None:
    shown = repr(config(tmp_path))
    assert SECRET not in shown and READ not in shown and WRITE not in shown


@pytest.mark.parametrize("raw", ["", "claude -p", "[]", '["ok", 1]', '{"cmd": "x"}'])
def test_the_command_must_be_a_json_list_of_strings(raw: str) -> None:
    with pytest.raises(ConfigError, match="JSON list of strings"):
        parse_command(raw)
    assert parse_command('["claude", "-p"]') == ("claude", "-p")


def test_argv_fills_only_its_two_placeholders() -> None:
    argv = argv_for(
        ["agent", "--mcp-config", "{mcp_config}", "--run={run_id}", "{token}"],
        "run-1",
        Path("/tmp/x/mcp.json"),
    )
    assert argv == ["agent", "--mcp-config", "/tmp/x/mcp.json", "--run=run-1", "{token}"]


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"run_id": "run-1", "kind": "drafting", "token": TOKEN}, True),
        ({"run_id": "run-1", "kind": "drafting", "token": TOKEN, "command": "sh"}, False),
        ({"run_id": "run 1", "kind": "drafting", "token": TOKEN}, False),
        ({"run_id": "run-1", "kind": "admin", "token": TOKEN}, False),
        ({"run_id": "run-1", "kind": "drafting", "token": "xoxb-not-ours"}, False),
        ({"run_id": "run-1", "kind": "drafting", "token": "jfr_a b"}, False),
    ],
)
def test_a_launch_request_is_exactly_three_fields(body: dict[str, Any], ok: bool) -> None:
    if ok:
        LaunchRequest.model_validate(body)
    else:
        with pytest.raises(ValueError):
            LaunchRequest.model_validate(body)


# The child environment


@pytest.mark.parametrize(
    ("kind", "token"),
    [
        (RunKind.DRAFTING, READ),
        (RunKind.QUESTION, READ),
        (RunKind.EXECUTION, WRITE),
    ],
)
def test_the_child_environment_is_the_allowlist(tmp_path: Path, kind: RunKind, token: str) -> None:
    env = child_env(config(tmp_path), kind, "run-1", tmp_path, ENVIRON)
    assert env["GITHUB_TOKEN"] == env["GH_TOKEN"] == token
    assert env["ANTHROPIC_API_KEY"] == "model-key"
    assert env["HOME"] == "/home/agent"
    assert env["TMPDIR"] == str(tmp_path)
    assert "UNLISTED" not in env
    for value in env.values():
        assert "should-never-appear" not in value
        assert SECRET not in value
    assert (READ if token == WRITE else WRITE) not in env.values()


def test_the_mcp_config_carries_the_token_as_a_header() -> None:
    assert mcp_config("http://jotflow:8765/mcp", TOKEN)["mcpServers"]["jotflow"] == {
        "type": "http",
        "url": "http://jotflow:8765/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }


# Launching for real, with the stand-in agent


@pytest.mark.parametrize(("kind", "token"), [(RunKind.DRAFTING, READ), (RunKind.EXECUTION, WRITE)])
async def test_a_run_gets_a_private_config_and_only_its_kinds_token(
    tmp_path: Path, kind: RunKind, token: str
) -> None:
    runner = Runner(config(tmp_path), environ=ENVIRON)
    await runner.launch(LaunchRequest(run_id="run-1", kind=kind, token=TOKEN))
    view = await finish(runner, "run-1")
    assert view == {"run_id": "run-1", "state": "exited", "exit_code": 0}
    seen = recorded(tmp_path)
    assert seen["config_mode"] == 0o600
    assert seen["dir_mode"] == 0o700
    assert seen["cwd"] == seen["run_dir"]
    assert seen["config"] == mcp_config("http://jotflow:8765/mcp", TOKEN)
    assert TOKEN not in json.dumps(seen["argv"]) and TOKEN not in json.dumps(seen["env"])
    assert seen["env"]["GITHUB_TOKEN"] == token
    assert "should-never-appear" not in json.dumps(seen["env"])
    assert SECRET not in json.dumps(seen["env"])
    # The run's directory, and the token in it, are gone once the run ends.
    assert not Path(seen["run_dir"]).exists()
    assert not list(tmp_path.glob(f"{DIR_PREFIX}*"))


async def test_a_failing_agent_reports_its_exit_code(tmp_path: Path) -> None:
    runner = Runner(config(tmp_path, mode="fail"), environ=ENVIRON)
    await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    assert (await finish(runner, "run-1"))["exit_code"] == 3


async def test_a_missing_token_refuses_without_falling_back(tmp_path: Path) -> None:
    from jotflow.adapters.runner import LaunchRefused

    runner = Runner(config(tmp_path, read_token=None), environ=ENVIRON)
    with pytest.raises(LaunchRefused, match="no GitHub token is configured for drafting runs"):
        await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    assert runner.get("run-1") is None


async def test_a_run_launches_once_and_live_runs_are_capped(tmp_path: Path) -> None:
    from jotflow.adapters.runner import LaunchRefused

    runner = Runner(config(tmp_path, mode="sleep", max_live=1), environ=ENVIRON)
    await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    with pytest.raises(LaunchRefused, match="already launched"):
        await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    with pytest.raises(LaunchRefused, match="limit of live runs"):
        await runner.launch(LaunchRequest(run_id="run-2", kind=RunKind.DRAFTING, token=TOKEN))
    await runner.close()
    assert runner.get("run-1")["state"] == "killed"  # type: ignore[index]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers; check its state.
    with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
        return handle.read().split()[2] != "Z"


async def _wait_recorded(tmp_path: Path) -> dict[str, Any]:
    for _ in range(500):
        if (tmp_path / "out.json").exists():
            try:
                return recorded(tmp_path)
            except ValueError:
                pass
        await asyncio.sleep(0.02)
    raise AssertionError("the agent did not start")


async def test_a_timed_out_run_loses_its_whole_process_group(tmp_path: Path) -> None:
    runner = Runner(
        config(tmp_path, mode="sleep"), environ=ENVIRON, limit=lambda _: 1.0, kill_grace=1.0
    )
    await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    grandchild = (await _wait_recorded(tmp_path))["grandchild"]
    view = await finish(runner, "run-1")
    assert view["state"] == "timed_out"
    await asyncio.sleep(0.2)
    assert not _alive(grandchild)
    assert not list(tmp_path.glob(f"{DIR_PREFIX}*"))


async def test_stopping_a_run_kills_it(tmp_path: Path) -> None:
    runner = Runner(config(tmp_path, mode="sleep"), environ=ENVIRON, kill_grace=1.0)
    await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    await _wait_recorded(tmp_path)
    view = await runner.stop("run-1")
    assert view is not None and view["state"] == RunState.KILLED.value
    assert await runner.stop("run-none") is None


async def test_an_agent_that_cannot_start_fails_cleanly(tmp_path: Path) -> None:
    runner = Runner(config(tmp_path, command=("/nonexistent/agent",)), environ=ENVIRON)
    await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    assert (await finish(runner, "run-1"))["state"] == "failed"
    assert not list(tmp_path.glob(f"{DIR_PREFIX}*"))


def test_leftover_run_directories_are_removed(tmp_path: Path) -> None:
    (tmp_path / f"{DIR_PREFIX}old").mkdir()
    (tmp_path / f"{DIR_PREFIX}old" / "mcp.json").write_text("{}")
    (tmp_path / "keep").mkdir()
    assert remove_stale(tmp_path) == 1
    assert (tmp_path / "keep").exists()


# The HTTP front door


@asynccontextmanager
async def http_for(runner: Runner, cfg: RunnerConfig) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=build_runner_app(runner, cfg)),
        base_url=f"http://{HOST}",
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as http:
        yield http


LAUNCH = {"run_id": "run-1", "kind": "drafting", "token": TOKEN}


@pytest.mark.parametrize(
    ("headers", "path", "status"),
    [
        ({"Origin": f"http://{HOST}"}, "/runs", 403),
        ({"Host": "evil.example:8766"}, "/runs", 403),
        ({}, "/runs?x=1", 400),
        ({"Authorization": "Bearer wrong"}, "/runs", 401),
        ({"Authorization": ""}, "/runs", 401),
    ],
)
async def test_the_gate_refuses_before_anything_starts(
    tmp_path: Path, headers: dict[str, str], path: str, status: int
) -> None:
    cfg = config(tmp_path)
    runner = Runner(cfg, environ=ENVIRON)
    async with http_for(runner, cfg) as http:
        response = await http.post(path, json=LAUNCH, headers=headers)
    assert response.status_code == status
    assert runner.get("run-1") is None


async def test_an_oversized_body_is_refused(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runner = Runner(cfg, environ=ENVIRON)
    async with http_for(runner, cfg) as http:
        response = await http.post("/runs", content=b" " * 5000)
    assert response.status_code == 413


async def test_launch_status_and_stop_over_http(tmp_path: Path) -> None:
    cfg = config(tmp_path, mode="sleep")
    runner = Runner(cfg, environ=ENVIRON, kill_grace=1.0)
    async with http_for(runner, cfg) as http:
        bad = await http.post("/runs", json={**LAUNCH, "command": "rm -rf /"})
        started = await http.post("/runs", json=LAUNCH)
        again = await http.post("/runs", json=LAUNCH)
        await _wait_recorded(tmp_path)
        status = await http.get("/runs/run-1")
        stopped = await http.delete("/runs/run-1")
        missing = await http.get("/runs/run-none")
        missing_stop = await http.delete("/runs/run-none")
    assert bad.status_code == 400
    assert (started.status_code, started.json()["run_id"]) == (202, "run-1")
    assert again.status_code == 409
    assert status.json()["state"] == "running"
    assert stopped.json()["state"] == "killed"
    assert (missing.status_code, missing_stop.status_code) == (404, 404)


# The agent reaches JotFlow's real MCP server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_launched_agent_reaches_the_mcp_server_with_its_token(tmp_path: Path) -> None:
    port = _free_port()
    registry = TokenRegistry()
    token = registry.issue(Grant("run-1", RunKind.DRAFTING, "C0EXAMPLE1", thread_ts="1.0"), 60)
    server = McpServer(build_app(port=port, tokens=registry), "127.0.0.1", port)
    await server.start()
    try:
        cfg = config(tmp_path, mode="mcp", mcp_url=f"http://127.0.0.1:{port}/mcp")
        runner = Runner(cfg, environ=ENVIRON)
        await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=token))
        view = await finish(runner, "run-1")
    finally:
        await server.stop()
    assert view["exit_code"] == 0
    seen = recorded(tmp_path)
    assert seen["mcp_status"] == 200
    assert seen["mcp_result"]["capabilities"] == {"tools": {"listChanged": False}}


# The subcommand


def test_the_runner_is_a_subcommand() -> None:
    assert "runner" in cli.COMMANDS


def test_runner_settings_build_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "JOTFLOW_RUNNER_SECRET": SECRET,
        "JOTFLOW_AGENT_COMMAND": '["claude", "-p", "--mcp-config", "{mcp_config}"]',
        "JOTFLOW_GITHUB_READ_TOKEN": READ,
        "JOTFLOW_AGENT_ENV_ALLOW": "ANTHROPIC_API_KEY, LANG",
        "JOTFLOW_RUNNER_ALLOWED_HOSTS": "agent,agent.internal",
    }.items():
        monkeypatch.setenv(name, value)
    cfg = runner_command.build_config(runner_command.RunnerSettings())  # type: ignore[call-arg]
    assert cfg.command[0] == "claude"
    assert cfg.github_token(RunKind.DRAFTING) == READ
    assert cfg.github_token(RunKind.EXECUTION) is None
    assert cfg.env_allow == ("ANTHROPIC_API_KEY", "LANG")
    assert {"agent:8766", "agent.internal:8766", "127.0.0.1:8766"} <= cfg.allowed_hosts


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({}, "JOTFLOW_RUNNER_SECRET"),
        ({"JOTFLOW_RUNNER_SECRET": SECRET, "JOTFLOW_AGENT_COMMAND": "claude"}, "JSON list"),
    ],
)
def test_runner_configuration_problems_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    problem: str,
) -> None:
    for name in ("JOTFLOW_RUNNER_SECRET", "JOTFLOW_AGENT_COMMAND"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert runner_command.run(argparse.Namespace(log_level="warning")) == 2
    assert problem in capsys.readouterr().err


def test_file_modes_constant() -> None:
    assert stat.S_IRUSR | stat.S_IWUSR == 0o600
