"""The codex preset's security gate (#22, #172), against the real Codex CLI, at no cost.

Codex runs with this preset's own generated config, pointed at a scripted fake
Responses API instead of OpenAI, with JotFlow's real MCP server and the fake
GitHub server attached. For each run kind it serves (drafting and question;
Codex never runs execution) it shows:

- Codex offers the model no shell or exec tool, only the known built-ins
- a scripted ``apply_patch`` call is refused, writes no file, and prompts no one
- Codex reaches JotFlow's MCP server

These tests run only where the pinned Codex release is on PATH, as in CI's
codex gate job; elsewhere they are skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from jotflow.adapters.presets import CODEX, CODEX_RUN_KINDS, CODEX_VERSION, RunContext
from jotflow.adapters.runner import write_private
from jotflow.models import RunKind
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore
from jotflow.testing.fake_responses_api import PROBE_FILE, FakeResponsesApi, apply_patch_call
from jotflow.testing.threads import builtin_thread
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.server import PATH, McpServer, bound_socket, build_app
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.write_tools import write_tools

CH = "C0EXAMPLE1"
POLICY = parse_policy(
    f'[channels.{CH}]\n[[channels.{CH}.repos]]\nrepo = "acme-co/web"\nnickname = "web"\n'
    'description = "Web"\n'
)
# The built-ins Codex 0.163.0 still offers with this config. A new one in a later
# release fails here, so a shell can never arrive unnoticed.
EXPECTED_BUILTINS = {
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
    "apply_patch",
    "tool_search",
}
SHELL_LIKE = {
    "shell",
    "local_shell",
    "exec_command",
    "unified_exec",
    "write_stdin",
    "container.exec",
}


def _pinned_codex() -> bool:
    codex = shutil.which("codex")
    if codex is None:
        return False
    done = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=30)
    return done.stdout.strip().endswith(CODEX_VERSION)


# CI's codex gate job sets this, so a missing or wrong Codex fails there instead of skipping.
REQUIRED = os.environ.get("JOTFLOW_REQUIRE_CODEX") == "1"
pytestmark = pytest.mark.skipif(
    not REQUIRED and not _pinned_codex(), reason=f"needs Codex {CODEX_VERSION}"
)


def test_the_pinned_codex_is_installed() -> None:
    assert _pinned_codex(), f"Codex {CODEX_VERSION} is not on PATH"
    assert CODEX.check_install() is None


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


@pytest.mark.parametrize("kind", sorted(CODEX_RUN_KINDS))
async def test_codex_offers_no_shell_and_refuses_apply_patch(
    kind: RunKind, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="jotflow.mcp.audit")
    store = MemoryStore()
    registry = TokenRegistry()
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
    try:
        context = RunContext(
            kind=kind,
            run_id="run-gate",
            run_dir=run_dir,
            config_path=run_dir / "mcp.json",
            mcp_url=f"http://127.0.0.1:{port}{PATH}",
            mcp_token=token,
            github_token="github_pat_gate",
            github_mcp=str(_fake_github(tmp_path)),
            model="gpt-5.5",
        )
        plan = CODEX.plan(context)
        for relative, text in plan.files.items():
            write_private(run_dir, relative, text)
        with FakeResponsesApi(first=apply_patch_call()) as api:
            # Only the model provider is overridden, so the rest is the preset's own config.
            argv = [*plan.argv[:2], *api.provider_overrides(), *plan.argv[2:]]
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(tmp_path / "home"),
                "TMPDIR": str(run_dir),
                "FAKE_MODEL_KEY": "gate",
                **plan.env,
            }
            done = await _run(argv, env, run_dir)
    finally:
        await server.stop()

    # No prompt held the run: it ended by itself, cleanly, within the timeout.
    assert done.returncode == 0, done.stderr[-2000:]
    offered = set(api.offered_tools())
    assert not offered & SHELL_LIKE
    assert offered == EXPECTED_BUILTINS
    [output] = api.tool_outputs()
    assert "rejected" in output and "read-only sandbox" in output
    assert not list(run_dir.rglob(PROBE_FILE)) and not (tmp_path / PROBE_FILE).exists()
    # Codex reached JotFlow's server and listed its tools.
    assert "method=tools/list" in caplog.text and "outcome=served" in caplog.text
    # Neither token reached the model.
    sent = json.dumps(api.requests)
    assert token not in sent and "github_pat_gate" not in sent


async def _run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    import asyncio

    def blocking() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    return await asyncio.to_thread(blocking)


async def test_a_code_mode_model_would_offer_an_exec_tool(tmp_path: Path) -> None:
    """Why the preset refuses other models: Codex 0.163.0 runs them in code mode,
    which adds a JavaScript exec tool and sub-agents whatever the config says."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    context = RunContext(
        kind=RunKind.DRAFTING,
        run_id="run-gate",
        run_dir=run_dir,
        config_path=run_dir / "mcp.json",
        mcp_url="http://127.0.0.1:9/mcp",
        mcp_token="jfr_gate",
        github_token="github_pat_gate",
        github_mcp=str(_fake_github(tmp_path)),
        model="gpt-6.1-sol",
    )
    plan = CODEX.plan(context)
    for relative, text in plan.files.items():
        write_private(run_dir, relative, text)
    with FakeResponsesApi() as api:
        # No JotFlow server here: only the model's own tool set is under test.
        skip = ["-c", "mcp_servers.jotflow.enabled=false"]
        argv = [*plan.argv[:2], *api.provider_overrides(), *skip, *plan.argv[2:]]
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(run_dir),
            "FAKE_MODEL_KEY": "gate",
            **plan.env,
        }
        await _run(argv, env, run_dir)
    extra = next(i for i in api.requests[0]["input"] if i.get("type") == "additional_tools")
    nested = {t["name"] for ns in extra["tools"] for t in ns.get("tools", [])}
    assert "exec" in nested and "spawn_agent" in nested


def _no_network_available() -> bool:
    """Whether this machine can run a command with no network: unshare -rn, unprivileged."""
    unshare = shutil.which("unshare")
    if unshare is None:
        return False
    done = subprocess.run([unshare, "-rn", "true"], capture_output=True, timeout=10)
    return done.returncode == 0


# CI's gate job allows unprivileged namespaces, so there a missing one fails, not skips.
@pytest.mark.skipif(
    not REQUIRED and not _no_network_available(),
    reason="needs unshare -rn to run Codex with no network",
)
async def test_the_preset_never_fetches_the_remote_model_catalog(tmp_path: Path) -> None:
    """Codex with an API key would fetch OpenAI's model catalog on every run, and that
    metadata sets each model's tools, so a remote change could add an exec tool (#284).

    Codex runs with the preset's own config and the built-in OpenAI provider, as a
    deployment does, but in a network namespace, so nothing leaves the machine. A
    control run with discovery forced back on shows the catalog fetch would be seen.
    """
    assert _no_network_available(), "unshare -rn is not available here"
    logs: dict[bool, str] = {}
    for discovery in (True, False):
        run_dir = tmp_path / f"run-{discovery}"
        run_dir.mkdir(mode=0o700)
        context = RunContext(
            kind=RunKind.DRAFTING,
            run_id="run-gate",
            run_dir=run_dir,
            config_path=run_dir / "mcp.json",
            mcp_url="http://127.0.0.1:9/mcp",
            mcp_token="jfr_gate",
            github_token="github_pat_gate",
            github_mcp=str(_fake_github(tmp_path)),
            model="gpt-5.5",
        )
        plan = CODEX.plan(context)
        for relative, text in plan.files.items():
            write_private(run_dir, relative, text)
        # No JotFlow server: only Codex's own start-up is under test.
        overrides = ["-c", "mcp_servers.jotflow.enabled=false"]
        if discovery:
            overrides += ["-c", "features.api_key_model_discovery=true"]
        argv = ["unshare", "-rn", *plan.argv[:2], *overrides, *plan.argv[2:]]
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(run_dir),
            "CODEX_API_KEY": "sk-gate-not-a-key",
            "RUST_LOG": "codex_models_manager=info",
            **plan.env,
        }
        logs[discovery] = await _offline(argv, env, run_dir)
    assert "list_models" in logs[True], "the control run should try to fetch the catalog"
    assert "list_models" not in logs[False], logs[False][-2000:]


async def _offline(argv: list[str], env: dict[str, str], cwd: Path) -> str:
    """Run until Codex gives up or 15 seconds pass, and return what it logged."""
    import asyncio

    def blocking() -> str:
        try:
            done = subprocess.run(
                argv,
                env=env,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired as stopped:
            out = stopped.stderr or b""
            return out.decode(errors="replace") if isinstance(out, bytes) else out
        return done.stderr

    return await asyncio.to_thread(blocking)
