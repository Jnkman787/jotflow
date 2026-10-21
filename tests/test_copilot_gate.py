"""The copilot preset's security gate (#59), against the real Copilot CLI, at no cost.

Copilot runs with this preset's own command and MCP config, pointed at a scripted
fake Responses API on 127.0.0.1 instead of a real provider, with JotFlow's real MCP
server and the fake GitHub server attached. For every run kind, execution included,
it shows:

- the model is offered exactly the kind's JotFlow and GitHub tools, nothing else
- a scripted ``bash`` or ``read_mcp_resource`` call is refused and writes no file
- Copilot ends by itself, and reaches JotFlow's MCP server
- neither token reaches the model
- under strace, Copilot contacts nothing but the fake model and JotFlow (no DNS,
  flags, model list or experiments), with the network reachable, and io_uring,
  which strace can't see into, never starts

It also pins two behaviours: an offered tool outside ``--allow-tool`` is denied
without a prompt, and a read-only-hinted tool outside the kind's list is never
offered, so ``--available-tools`` stays the boundary.

These tests run only where the pinned Copilot is on PATH. CI's copilot gate job
sets ``JOTFLOW_REQUIRE_COPILOT=1``, so there a missing Copilot or strace fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jotflow.adapters.presets import (
    COPILOT,
    COPILOT_VERSION,
    GITHUB_TOOLS,
    JOTFLOW_TOOLS,
    RunContext,
)
from jotflow.models import RUN_KINDS, RunKind
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore
from jotflow.testing.fake_responses_api import FakeResponsesApi, function_call
from jotflow.testing.threads import builtin_thread
from jotflow.whiteboard.doc_tools import DOC_TOOLS
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.server import PATH, McpServer, bound_socket, build_app
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.write_tools import write_tools
from tests.outbound_trace import refused_contacts, strace_argv, trace_lines

CH = "C0EXAMPLE1"
POLICY = parse_policy(
    f'[channels.{CH}]\n[[channels.{CH}.repos]]\nrepo = "acme-co/web"\nnickname = "web"\n'
    'description = "Web"\n'
)
GITHUB_TOKEN = "github_pat_gate"
MODEL = "gpt-5.5"
PROBE_FILE = "jotflow-copilot-probe"
REFUSED = "does not exist"
REQUIRED = os.environ.get("JOTFLOW_REQUIRE_COPILOT") == "1"
STRACE = shutil.which("strace")


def _pinned_copilot() -> bool:
    return shutil.which("copilot") is not None and COPILOT.check_install() is None


pytestmark = pytest.mark.skipif(
    not REQUIRED and not _pinned_copilot(), reason=f"needs Copilot CLI {COPILOT_VERSION}"
)


def test_the_pinned_copilot_and_strace_are_installed() -> None:
    assert COPILOT.check_install() is None, f"Copilot CLI {COPILOT_VERSION} is not on PATH"
    if REQUIRED:
        assert STRACE is not None, "the gate's network check needs strace"


class NeverPosts:
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        raise AssertionError("the gate posts nothing")


def _fake_github(root: Path) -> Path:
    data = root / "github.json"
    repos = builtin_thread("feature-request").github
    data.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in repos.items()}))
    wrapper = root / "github-mcp-server"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -I -m jotflow.testing.fake_github_mcp'
        f' --data "{data}" "$@"\n'
    )
    wrapper.chmod(stat.S_IRWXU)
    return wrapper


def offered_for(kind: RunKind) -> list[str]:
    # The gate's policy sets no read_paths, so JotFlow serves no doc tools (#209).
    served = [t for t in JOTFLOW_TOOLS[kind] if t not in DOC_TOOLS]
    return sorted([f"jotflow-{t}" for t in served] + [f"github-{t}" for t in GITHUB_TOOLS[kind]])


@dataclass
class GateRun:
    done: subprocess.CompletedProcess[str]
    api: FakeResponsesApi
    audit: str
    token: str
    trace: list[str] | None
    allowed_ports: set[int]
    probe: Path

    def offered(self) -> list[str]:
        assert self.api.requests, self.done.stderr[-2000:]
        return sorted(self.api.offered_tools())

    def output(self) -> str:
        [result] = self.api.tool_outputs()
        return result

    def strangers(self) -> list[str]:
        """Every contact but the fake model's and JotFlow's on loopback, and any live
        io_uring, through the codex gate's shared rules (#290)."""
        return refused_contacts(self.trace or [], self.allowed_ports)


Mutate = Callable[[list[str], dict[str, Any]], None]


async def gate_run(
    kind: RunKind,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    first: dict[str, Any],
    mutate: Mutate | None = None,
) -> GateRun:
    caplog.set_level("INFO", logger="jotflow.mcp.audit")
    store = MemoryStore()
    registry = TokenRegistry()
    if kind is RunKind.EXECUTION:
        grant = Grant("run-gate", kind, CH, proposal_id="p-gate")
    else:
        grant = Grant("run-gate", kind, CH, thread_ts="1792310400.000100")
    token = registry.issue(grant, 600)
    sock = bound_socket()  # never released, so no other process can take the port (#184)
    port = int(sock.getsockname()[1])
    tools = (*read_tools(store, POLICY), *write_tools(store, POLICY, NeverPosts()))
    server = McpServer(
        build_app(port=port, tokens=registry, tools=tools), "127.0.0.1", port, sock=sock
    )
    await server.start()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    probe = tmp_path / PROBE_FILE
    prefix = tmp_path / "trace" / "strace"
    prefix.parent.mkdir()
    try:
        context = RunContext(
            kind=kind,
            run_id="run-gate",
            run_dir=run_dir,
            config_path=run_dir / "mcp.json",
            mcp_url=f"http://127.0.0.1:{port}{PATH}",
            mcp_token=token,
            github_token=GITHUB_TOKEN,
            github_mcp=str(_fake_github(tmp_path)),
            model=MODEL,
        )
        plan = COPILOT.plan(context)
        argv = list(plan.argv)
        config = json.loads(json.dumps(plan.config))
        if mutate is not None:
            mutate(argv, config)
        context.config_path.write_text(json.dumps(config))
        context.config_path.chmod(0o600)
        with FakeResponsesApi(first=first, full_stream=True) as api:
            model_port = int(api.base_url.rsplit(":", 1)[1].split("/")[0])
            # Only the model provider is the gate's own: the rest is the preset's.
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(tmp_path / "home"),
                "TMPDIR": str(run_dir),
                **plan.env,
                "COPILOT_PROVIDER_BASE_URL": api.base_url,
                "COPILOT_PROVIDER_TYPE": "openai",
                "COPILOT_PROVIDER_WIRE_API": "responses",
                "COPILOT_PROVIDER_API_KEY": "gate",
            }
            if STRACE is not None:
                argv = [*strace_argv(STRACE, prefix), *argv]
            done = await asyncio.to_thread(
                subprocess.run,
                argv,
                env=env,
                cwd=run_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
            )
    finally:
        await server.stop()
    trace = trace_lines(prefix) if STRACE is not None else None
    return GateRun(done, api, caplog.text, token, trace, {port, model_port}, probe)


def assert_clean(run: GateRun, kind: RunKind) -> None:
    # No prompt held the run: it ended by itself, cleanly, within the timeout.
    assert run.done.returncode == 0, run.done.stderr[-2000:]
    assert run.offered() == offered_for(kind)
    assert "method=tools/list" in run.audit and "outcome=served" in run.audit
    sent = json.dumps(run.api.requests)
    assert run.token not in sent and GITHUB_TOKEN not in sent
    assert not run.probe.exists()
    if STRACE is not None:
        assert any("connect(" in line for line in run.trace or []), "strace saw no connection"
        assert run.strangers() == [], run.strangers()


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
async def test_copilot_offers_exactly_the_kinds_tools_and_refuses_a_shell(
    kind: RunKind, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probe = {"command": f"touch {tmp_path / PROBE_FILE}", "description": "probe"}
    run = await gate_run(kind, tmp_path, caplog, function_call("bash", probe))
    assert_clean(run, kind)
    assert REFUSED in run.output() and "'bash'" in run.output()


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
async def test_copilot_has_no_resource_read_tool(
    kind: RunKind, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probe = {"server": "github", "uri": "repo://acme-co/web/contents/README.md"}
    run = await gate_run(kind, tmp_path, caplog, function_call("read_mcp_resource", probe))
    assert_clean(run, kind)
    assert REFUSED in run.output()


async def test_an_allowed_tool_runs_without_a_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = await gate_run(RunKind.DRAFTING, tmp_path, caplog, function_call("jotflow-get_work"))
    assert_clean(run, RunKind.DRAFTING)
    # JotFlow ran it and answered: the gate's run isn't in the store, so get_work says so.
    assert "method=tools/call tool=get_work run=run-gate status=200 outcome=served" in run.audit
    assert "run run-gate is unknown" in run.output() and "Permission denied" not in run.output()


async def test_an_offered_tool_outside_allow_tool_is_denied_without_a_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def narrow(argv: list[str], config: dict[str, Any]) -> None:
        at = argv.index("jotflow(ask_channel)")
        del argv[at - 1 : at + 1]

    run = await gate_run(
        RunKind.DRAFTING,
        tmp_path,
        caplog,
        function_call("jotflow-ask_channel", {"question": "Which repo?"}),
        narrow,
    )
    assert_clean(run, RunKind.DRAFTING)
    assert "Permission denied" in run.output()
    assert "tool=ask_channel" not in run.audit


async def test_a_read_only_tool_outside_the_kind_is_never_offered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``--available-tools`` is the boundary, not ``--allow-tool``: Copilot would run an
    offered read-only-hinted tool unasked, so one outside the kind must never be offered.
    """

    def widen(argv: list[str], config: dict[str, Any]) -> None:
        github = config["mcpServers"]["github"]
        github["args"] = ["stdio"]  # the server lists every tool, search_issues included
        github["tools"] = ["*"]

    run = await gate_run(
        RunKind.EXECUTION,
        tmp_path,
        caplog,
        function_call("github-search_issues", {"query": "repo:acme-co/web bar"}),
        widen,
    )
    assert "search_issues" not in GITHUB_TOOLS[RunKind.EXECUTION]
    assert_clean(run, RunKind.EXECUTION)
    assert REFUSED in run.output()
