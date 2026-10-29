"""Replay a fixture's approved proposals through execution runs (#31).

``jotflow replay --execute --agent <preset>`` checks that an approved proposal
reaches GitHub exactly as approved. For each fixture it plays the scripted
drafting runs in-process, then for each proposal they made:

- approves it, as the Approve button would
- runs its execution run through the real exec runner and JotFlow's real MCP
  server, with :mod:`jotflow.testing.fake_github_mcp` as GitHub, writes on
- compares every write the fake server logged with the writes JotFlow planned
  and served in ``get_work``, exactly, and checks ``record_result`` made the
  proposal applied with the right issue URL

It does that twice. The second time, an interrupted first attempt writes and
exits without reporting, and the run under test must find the hidden marker,
write nothing, and report the issue the first attempt wrote. A proposal whose
labels the repo lacks must fail with the missing labels and write nothing.

``--agent scripted`` runs :mod:`jotflow.testing.scripted_executor`, so CI checks
the whole path without a model.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jotflow.adapters.presets import (
    PRESETS,
    Preset,
    RunContext,
    RunPlan,
    github_server,
    jotflow_server,
)
from jotflow.adapters.runner import EXECUTION_KINDS, LaunchRequest, Runner, RunnerConfig, RunState
from jotflow.execution.attribution import execution_footer
from jotflow.execution.writes import PlannedWrite, with_kept_markers
from jotflow.execution.writes import plan as plan_writes
from jotflow.models import ProposalRecord, ProposalState, RunKind, RunStatus
from jotflow.replay import Check, Prepared, ReplayError, ReplayReport, prepare, run_script
from jotflow.replay_agent import (
    AGENTS,
    FIXTURE_GITHUB_TOKEN,
    SCRIPTED,
    AgentOptions,
    _fake_github,
    _usage_wrapper,
    _wait,
    check_env,
)
from jotflow.store.hashing import content_hash
from jotflow.testing.threads import GithubRepo, SavedThread
from jotflow.whiteboard.server import PATH, McpServer, bound_socket, build_app

APPROVER = "U0REPLAY"


@dataclass(frozen=True)
class StandInPreset:
    """The scripted executor, configured like a real preset: JotFlow plus the
    GitHub server, with the run kind's tools, and no GitHub token in its env."""

    crash_after_write: bool = False
    tamper: bool = False  # for tests: an agent that changes what it writes
    skip_marker_check: bool = False  # for tests: an agent that writes twice
    name: str = SCRIPTED
    default_models: Mapping[RunKind, str] = field(default_factory=dict)
    needs_github_mcp: bool = False
    github_token_in_config: bool = True

    def plan(self, run: RunContext) -> RunPlan:
        argv = [sys.executable, "-m", "jotflow.testing.scripted_executor", str(run.config_path)]
        if self.crash_after_write:
            argv.append("--crash-after-write")
        if self.tamper:
            argv.append("--tamper")
        if self.skip_marker_check:
            argv.append("--skip-marker-check")
        config = {
            "mcpServers": {
                "jotflow": jotflow_server(run.mcp_url, run.mcp_token),
                "github": github_server(run),
            }
        }
        return RunPlan(argv=tuple(argv), config=config, github_token_env=False)


def _executor(options: AgentOptions, *, crash: bool = False) -> Preset:
    if crash or options.agent == SCRIPTED:
        return StandInPreset(crash_after_write=crash)
    try:
        return PRESETS[options.agent]
    except KeyError:
        raise ReplayError(f"no agent {options.agent}; choose one of {', '.join(AGENTS)}") from None


@dataclass
class _Launch:
    prepared: Prepared
    options: AgentOptions
    root: Path
    url: str
    github: Path
    environ: dict[str, str]

    async def run(self, record: ProposalRecord, preset: Preset) -> tuple[bool, str]:
        lifecycle, store = self.prepared.lifecycle, self.prepared.state.store
        await store.enqueue_run(
            RunKind.EXECUTION, record.channel_id, record.thread_ts, proposal_id=record.id
        )
        started = await lifecycle.start_next(max_running=8, kinds={RunKind.EXECUTION})
        if started is None:
            raise ReplayError(f"the execution run for proposal {record.id} did not start")
        config = RunnerConfig(
            secret="replay-" + "x" * 32,
            mcp_url=self.url,
            preset=preset,
            kinds=EXECUTION_KINDS,
            write_token=FIXTURE_GITHUB_TOKEN,
            env_allow=tuple(n for n in self.options.env_allow if n in self.environ),
            allowed_hosts=frozenset(),
            workdir=self.root,
            models={k: v for k, v in self.options.models.items() if k is RunKind.EXECUTION},
            github_mcp=str(self.github),
        )
        check_env(preset, config, self.environ)
        runner = Runner(config, environ=self.environ, limit=lambda _k: self.options.time_limit)
        await runner.launch(
            LaunchRequest(run_id=started.run.id, kind=RunKind.EXECUTION, token=started.token)
        )
        view = await _wait(runner, started.run.id)
        ok = view["state"] == RunState.EXITED.value and view["exit_code"] == 0
        await lifecycle.finish(
            started.run.id,
            RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            error=None if ok else "agent did not exit cleanly",
        )
        return ok, f"{view['state']} exit {view['exit_code']}"


def _logged(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _expected(write: PlannedWrite, repos: dict[str, GithubRepo], repo: str) -> dict[str, Any]:
    """The arguments the write must have, with any current-set change applied."""
    arguments = dict(write.arguments)
    number = arguments.get("issue_number")
    issue = next((i for i in repos[repo].issues if i.number == number), None)
    for key, change in (
        ("labels", write.labels_from_current),
        ("assignees", write.assignees_from_current),
    ):
        if change is None:
            continue
        current = list(getattr(issue, key, ())) if issue is not None else []
        wanted = [v for v in current if v not in change.remove]
        arguments[key] = sorted({*wanted, *change.add})
    if write.markers_from_current:
        arguments["body"] = with_kept_markers(arguments["body"], issue.body if issue else "")
    return arguments


def _same(logged: dict[str, Any], tool: str, expected: dict[str, Any]) -> bool:
    if logged.get("tool") != tool:
        return False
    got = dict(logged.get("arguments") or {})
    for key in ("labels", "assignees"):  # a set, in any order
        if key in got and key in expected and isinstance(got[key], list):
            got[key] = sorted(got[key])
    return got == expected


def _new_number(repos: dict[str, GithubRepo], repo: str) -> int:
    return max((i.number for i in repos[repo].issues), default=0) + 1


async def _approved(prepared: Prepared) -> list[ProposalRecord]:
    state = prepared.state
    channel = next(iter(state.thread.policy.channels))
    records: list[ProposalRecord] = []
    for root in sorted({run.thread_ts for run in state.thread.agent}):
        for record in await state.store.thread_proposals(channel, root):
            if record.state is ProposalState.PENDING:
                records.append(
                    await state.store.transition(
                        record.id,
                        ProposalState.APPROVED,
                        expected={ProposalState.PENDING},
                        approved_by=APPROVER,
                        expected_hash=content_hash(record.proposal),
                    )
                )
    return records


async def _scenario(
    thread: SavedThread, options: AgentOptions, *, interrupted: bool
) -> list[Check]:
    name = "after an interrupted attempt" if interrupted else "first attempt"
    prepared = await prepare(thread)
    state = prepared.state
    await run_script(state, prepared.lifecycle, prepared.registry, state.accepted)
    records = await _approved(prepared)
    if not records:
        return [Check(f"execute ({name})", True, "no proposals to execute")]
    sock = bound_socket()  # held, never released, so nothing else can take the port (#184)
    port = int(sock.getsockname()[1])
    app = build_app(port=port, tokens=prepared.registry, tools=list(state.specs.values()))
    server = McpServer(app, "127.0.0.1", port, sock=sock)
    await server.start()
    problems: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="jotflow-execute-") as tmp:
            root = Path(tmp)
            root.chmod(stat.S_IRWXU)
            log = root / "writes.jsonl"
            environ = dict(os.environ)
            environ.setdefault("HOME", str(root))
            if options.agent == "claude-code" and options.usage_file is not None:
                folder = _usage_wrapper(root, options.usage_file)
                environ["PATH"] = f"{folder}{os.pathsep}{environ.get('PATH', '')}"
            launch = _Launch(
                prepared,
                options,
                root,
                f"http://127.0.0.1:{port}{PATH}",
                _fake_github(thread, root, log=log),
                environ,
            )
            repos = dict(thread.github)
            for record in records:
                problems += await _execute_one(launch, record, repos, log, interrupted=interrupted)
    except ReplayError as exc:
        problems.append(str(exc))
    finally:
        await server.stop()
    return [
        Check(
            f"execute ({name})",
            not problems,
            "; ".join(problems) if problems else f"{len(records)} proposal(s) handled as planned",
        )
    ]


async def _execute_one(
    launch: _Launch,
    record: ProposalRecord,
    repos: dict[str, GithubRepo],
    log: Path,
    *,
    interrupted: bool,
) -> list[str]:
    state = launch.prepared.state
    repo = record.proposal.repo
    if repo not in repos:
        return [f"{record.id}: the fixture has no github block for {repo}"]
    tail = await execution_footer(record, state.thread.policy, state.attribution, state.store)
    checks, writes = plan_writes(record, tail)
    missing = sorted(set(checks.labels) - set(repos[repo].labels))
    expected = [(w.tool, _expected(w, repos, repo)) for w in writes]
    number = getattr(record.proposal, "issue_number", None) or _new_number(repos, repo)
    url = f"https://github.com/{repo}/issues/{number}"
    before = len(_logged(log))
    if interrupted and not missing:
        await launch.run(record, _executor(launch.options, crash=True))
        before = len(_logged(log))
        if before == 0:
            return [f"{record.id}: the interrupted attempt wrote nothing"]
    ok, how = await launch.run(record, _executor(launch.options))
    problems = [] if ok else [f"{record.id}: the agent ended {how}"]
    written = _logged(log)
    done = await state.store.get_proposal(record.id)
    result = done.result if done is not None else None
    if missing:
        if written[before:]:
            problems.append(f"{record.id}: wrote although labels {missing} are missing")
        error = (result.error or "") if result else ""
        if done is None or done.state is ProposalState.APPLIED or "labels missing" not in error:
            problems.append(f"{record.id}: did not report the missing labels {missing}")
        return problems
    if interrupted:
        if len(written) != before:
            problems.append(f"{record.id}: wrote again after an interrupted attempt")
    elif len(written) - before != len(expected) or not all(
        _same(got, tool, want)
        for got, (tool, want) in zip(written[before:], expected, strict=False)
    ):
        problems.append(f"{record.id}: the writes differ from the planned ones")
    if done is None or done.state is not ProposalState.APPLIED:
        problems.append(f"{record.id}: is not applied")
    elif result is None or result.issue_url != url:
        problems.append(f"{record.id}: reported the wrong issue URL")
    # The next proposal sees what this one wrote.
    data = json.loads((launch.root / "github.json").read_text(encoding="utf-8"))
    repos.update({n: GithubRepo.model_validate(r) for n, r in data.items()})
    return problems


async def replay_execution(thread: SavedThread, options: AgentOptions) -> ReplayReport:
    """Execute ``thread``'s proposals twice, cleanly and after an interruption."""
    report = ReplayReport(thread.name, thread.description)
    report.checks += await _scenario(thread, options, interrupted=False)
    report.checks += await _scenario(thread, options, interrupted=True)
    return report
