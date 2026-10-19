"""Replay a saved thread with a real agent in place of its script (#23).

``jotflow replay --agent <preset>`` runs each of the fixture's drafting runs
the way production does, end to end on this machine:

- JotFlow's real MCP server, in-process on ``127.0.0.1``, over the replayed
  store, the fixture-backed Slack reads, and the real tools
- the run lifecycle's real token for each run
- the real runner (:mod:`jotflow.adapters.runner`), which launches the preset
  with its private 0600 MCP config, its env allowlist, and its time limit
- for GitHub, :mod:`jotflow.testing.fake_github_mcp` in place of
  github-mcp-server, serving the fixture's ``github`` block

Then it compares the store with the fixture's ``expected`` outcome, as
scripted replay does. The agent's model and login come from the caller's own
environment: only the names in ``env_allow`` reach it. ``scripted`` is a
stand-in agent that plays the fixture's script over MCP, so tests and CI run
the whole harness without a model.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jotflow.adapters.presets import PRESETS, CommandPreset, Preset
from jotflow.adapters.runner import LIVE, LaunchRequest, Runner, RunnerConfig, RunState
from jotflow.models import RunKind, RunStatus
from jotflow.replay import Check, Prepared, ReplayError, ReplayReport, finish, prepare
from jotflow.store import Store
from jotflow.testing.threads import TOPIC_KEY_PREFIX, SavedThread
from jotflow.whiteboard.server import PATH, McpServer, build_app

SCRIPTED = "scripted"
AGENTS = (SCRIPTED, *sorted(PRESETS))
DEFAULT_TIME_LIMIT = 600.0
# The fake GitHub server takes no real token; the runner still requires one.
FIXTURE_GITHUB_TOKEN = "github_pat_fixture"
# What a model login usually needs; pass more with env_allow.
DEFAULT_ENV_ALLOW = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "HTTPS_PROXY", "NO_PROXY")


# Wraps the claude CLI during replay to keep its result's numbers, nothing else:
# no text, so nothing the agent read or wrote is kept.
_USAGE_WRAPPER = """#!{python}
import json, subprocess, sys
done = subprocess.run([{claude!r}, *sys.argv[1:], "--output-format", "json"],
                      stdin=subprocess.DEVNULL, capture_output=True, text=True)
keep = {{"exit": done.returncode}}
try:
    result = json.loads(done.stdout)
except ValueError:
    result = {{}}
if isinstance(result, dict):
    for key in ("num_turns", "duration_ms", "total_cost_usd"):
        if isinstance(result.get(key), (int, float)):
            keep[key] = result[key]
    usage = result.get("usage")
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens"):
            if isinstance(usage.get(key), int):
                keep[key] = usage[key]
with open({usage!r}, "a") as out:
    out.write(json.dumps(keep) + "\\n")
sys.exit(done.returncode)
"""
USAGE_FIELDS = (
    "num_turns",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_cost_usd",
)


@dataclass(frozen=True)
class AgentOptions:
    agent: str
    thread_ref: str  # what the scripted agent loads: a built-in name or a path
    env_allow: tuple[str, ...] = DEFAULT_ENV_ALLOW
    time_limit: float = DEFAULT_TIME_LIMIT
    models: Mapping[RunKind, str] = field(default_factory=dict)
    usage_file: Path | None = None  # claude-code: where its result's numbers are appended


def _preset(options: AgentOptions, index: int) -> Preset:
    if options.agent == SCRIPTED:
        return CommandPreset(
            (
                sys.executable,
                "-m",
                "jotflow.testing.scripted_agent",
                "--thread",
                options.thread_ref,
                "--run",
                str(index),
                "{mcp_config}",
            )
        )
    try:
        return PRESETS[options.agent]
    except KeyError:
        raise ReplayError(f"no agent {options.agent}; choose one of {', '.join(AGENTS)}") from None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _fake_github(thread: SavedThread, root: Path) -> Path:
    """Write the fixture's github block and a wrapper the preset starts as its server."""
    data = root / "github.json"
    data.write_text(
        json.dumps({name: repo.model_dump(mode="json") for name, repo in thread.github.items()}),
        encoding="utf-8",
    )
    wrapper = root / "github-mcp-server"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -I -m jotflow.testing.fake_github_mcp --data "{data}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(stat.S_IRWXU)
    return wrapper


def _usage_wrapper(root: Path, usage: Path) -> Path:
    """A ``claude`` ahead of the real one on PATH that records its result's numbers."""
    real = shutil.which("claude")
    if real is None:
        raise ReplayError("the claude CLI is not on PATH")
    folder = root / "bin"
    folder.mkdir(mode=0o700)
    wrapper = folder / "claude"
    wrapper.write_text(
        _USAGE_WRAPPER.format(python=sys.executable, claude=real, usage=str(usage)),
        encoding="utf-8",
    )
    wrapper.chmod(stat.S_IRWXU)
    return folder


def usage_totals(path: Path) -> dict[str, float]:
    """Sum the numbers the wrapper recorded, plus how many agent runs it saw."""
    totals: dict[str, float] = {"runs": 0, **dict.fromkeys(USAGE_FIELDS, 0)}
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        totals["runs"] += 1
        for key in USAGE_FIELDS:
            totals[key] += record.get(key, 0)
    return totals


async def _wait(runner: Runner, run_id: str) -> dict[str, object]:
    while True:
        view = runner.get(run_id)
        if view is not None and RunState(str(view["state"])) not in LIVE:
            return view
        await asyncio.sleep(0.1)


async def _run_agent(prepared: Prepared, options: AgentOptions, root: Path, url: str) -> None:
    state, report = prepared.state, prepared.report
    github = _fake_github(state.thread, root)
    environ = dict(os.environ)
    environ.setdefault("HOME", str(root))
    if options.agent == "claude-code" and options.usage_file is not None:
        environ["PATH"] = (
            f"{_usage_wrapper(root, options.usage_file)}{os.pathsep}{environ.get('PATH', '')}"
        )
    failed: list[str] = []
    max_running = state.thread.policy.agent.max_concurrent_runs
    for index, run in enumerate(state.thread.agent):
        await state.store.enqueue_run(RunKind.DRAFTING, run.channel, run.thread_ts)
        started = await prepared.lifecycle.start_next(max_running=max_running)
        if started is None:
            raise ReplayError(f"the run for thread {run.thread_ts} did not start")
        preset = _preset(options, index)
        config = RunnerConfig(
            secret="replay-" + "x" * 32,
            mcp_url=url,
            preset=preset,
            read_token=FIXTURE_GITHUB_TOKEN,
            env_allow=tuple(n for n in options.env_allow if n in environ),
            allowed_hosts=frozenset(),
            workdir=root,
            models={k: v for k, v in options.models.items() if k is RunKind.DRAFTING},
            github_mcp=str(github),
        )
        runner = Runner(config, environ=environ, limit=lambda _kind: options.time_limit)
        await runner.launch(
            LaunchRequest(run_id=started.run.id, kind=RunKind.DRAFTING, token=started.token)
        )
        view = await _wait(runner, started.run.id)
        ok = view["state"] == RunState.EXITED.value and view["exit_code"] == 0
        if not ok:
            failed.append(f"{run.thread_ts} {view['state']} exit {view['exit_code']}")
        await prepared.lifecycle.finish(
            started.run.id,
            RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            error=None if ok else "agent did not exit cleanly",
        )
    total = len(state.thread.agent)
    report.checks.append(
        Check(
            f"agent {options.agent}",
            not failed,
            "; ".join(failed) if failed else f"{total} run(s) exited cleanly",
        )
    )


async def _name_new_topics(prepared: Prepared) -> None:
    """Map each expected new topic's key to the topic its first message landed in."""
    state = prepared.state
    channel = next(iter(state.thread.policy.channels))
    for want in state.thread.expected.topics:
        if not want.topic.startswith(TOPIC_KEY_PREFIX) or want.topic in state.topics:
            continue
        for ts in want.messages:
            link = await state.store.get_message(channel, ts)
            if link is not None and link.topic_id is not None:
                state.topics[want.topic] = link.topic_id
                break


async def replay_with_agent(
    thread: SavedThread, options: AgentOptions, *, store: Store | None = None
) -> ReplayReport:
    """Run ``thread``'s drafting runs with a real agent and compare the outcome."""
    prepared = await prepare(thread, store=store)
    port = _free_port()
    specs = list(prepared.state.specs.values())
    server = McpServer(
        build_app(port=port, tokens=prepared.registry, tools=specs), "127.0.0.1", port
    )
    await server.start()
    try:
        with tempfile.TemporaryDirectory(prefix="jotflow-replay-") as tmp:
            root = Path(tmp)
            root.chmod(stat.S_IRWXU)
            try:
                await _run_agent(prepared, options, root, f"http://127.0.0.1:{port}{PATH}")
            except ReplayError as exc:
                prepared.report.checks.append(Check(f"agent {options.agent}", False, str(exc)))
    finally:
        await server.stop()
    await _name_new_topics(prepared)
    return await finish(prepared)


@dataclass
class Tally:
    """How often each check passed over repeated runs of one fixture."""

    thread: str
    runs: int = 0
    passed: int = 0
    checks: dict[str, int] = field(default_factory=dict)

    def add(self, report: ReplayReport) -> None:
        self.runs += 1
        self.passed += report.ok
        for check in report.checks:
            self.checks[check.name] = self.checks.get(check.name, 0) + (check.ok is not False)

    def render(self) -> str:
        lines = [f"{self.thread}: {self.passed}/{self.runs} runs matched"]
        lines += [
            f"  {name}: {ok}/{self.runs}" for name, ok in self.checks.items() if ok < self.runs
        ]
        return "\n".join(lines)


async def repeat(
    thread: SavedThread, options: AgentOptions, times: int
) -> tuple[Tally, list[ReplayReport]]:
    tally = Tally(thread.name)
    reports = []
    for _ in range(times):
        report = await replay_with_agent(thread, options)
        tally.add(report)
        reports.append(report)
    return tally, reports
