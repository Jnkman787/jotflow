"""The copilot preset (#59): Copilot CLI, offline, with the team's own model key."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jotflow.adapters import presets
from jotflow.adapters.isolation import API_KEYS
from jotflow.adapters.presets import (
    COPILOT,
    COPILOT_PROMPTS,
    COPILOT_PROVIDER_ENV,
    COPILOT_VERSION,
    GITHUB_TOOLS,
    JOTFLOW_TOOLS,
)
from jotflow.adapters.runner import (
    EXECUTION_KINDS,
    ConfigError,
    LaunchRequest,
    Runner,
    RunState,
    parse_preset,
)
from jotflow.commands import runner as runner_command
from jotflow.models import PARTIAL_PRESET_KINDS, RUN_KINDS, RunKind
from jotflow.replay import ReplayError
from jotflow.replay_agent import check_env as replay_check_env
from tests.test_presets import READ, SECRET, TOKEN, WRITE, context, finished, preset_config

MODEL = "gpt-5.5"
PROVIDER = {
    "COPILOT_PROVIDER_BASE_URL": "https://api.openai.com/v1",
    "COPILOT_PROVIDER_TYPE": "openai",
    "COPILOT_PROVIDER_WIRE_API": "responses",
    "COPILOT_PROVIDER_API_KEY": "sk-test",
}
ALLOW = COPILOT_PROVIDER_ENV


def plan_for(kind: RunKind, tmp_path: Path) -> presets.RunPlan:
    return COPILOT.plan(context(kind, tmp_path, model=MODEL))


def test_copilot_is_an_experimental_preset_for_every_kind() -> None:
    assert parse_preset("copilot") is COPILOT
    assert "copilot" in presets.EXPERIMENTAL
    # Unlike codex, it offers no resource-read tools, so it may serve execution.
    assert "copilot" not in PARTIAL_PRESET_KINDS
    assert API_KEYS["copilot"] == ("COPILOT_PROVIDER_API_KEY",)


def test_copilot_drafting_argv_is_pinned(tmp_path: Path) -> None:
    plan = plan_for(RunKind.DRAFTING, tmp_path)
    allowed = [
        *(x for t in JOTFLOW_TOOLS[RunKind.DRAFTING] for x in ("--allow-tool", f"jotflow({t})")),
        *(x for t in GITHUB_TOOLS[RunKind.DRAFTING] for x in ("--allow-tool", f"github({t})")),
    ]
    assert plan.argv == (
        "copilot",
        "-p",
        COPILOT_PROMPTS[RunKind.DRAFTING],
        "--additional-mcp-config",
        f"@{tmp_path / 'mcp.json'}",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
        "--no-ask-user",
        "--no-auto-update",
        "--disable-client-telemetry",
        "--log-level",
        "none",
        "--model",
        MODEL,
        *allowed,
        "--available-tools",
        *(f"jotflow-{t}" for t in JOTFLOW_TOOLS[RunKind.DRAFTING]),
        *(f"github-{t}" for t in GITHUB_TOOLS[RunKind.DRAFTING]),
    )


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
def test_copilot_offers_and_allows_exactly_the_kinds_tools(kind: RunKind, tmp_path: Path) -> None:
    argv = plan_for(kind, tmp_path).argv
    start = argv.index("--available-tools") + 1
    offered = list(argv[start:])
    expected = [f"jotflow-{t}" for t in JOTFLOW_TOOLS[kind]]
    expected += [f"github-{t}" for t in GITHUB_TOOLS[kind]]
    assert offered == expected
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-tool"]
    assert allowed == [f"jotflow({t})" for t in JOTFLOW_TOOLS[kind]] + [
        f"github({t})" for t in GITHUB_TOOLS[kind]
    ]
    assert not any("*" in name for name in offered + allowed)
    assert "--allow-all-tools" not in argv and "--yolo" not in argv and "--allow-all" not in argv


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
def test_copilot_loads_only_jotflow_and_the_pinned_github_server(
    kind: RunKind, tmp_path: Path
) -> None:
    servers = plan_for(kind, tmp_path).config["mcpServers"]
    assert servers["jotflow"] == {
        "type": "http",
        "url": "http://jotflow:8765/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN}"},
        "tools": list(JOTFLOW_TOOLS[kind]),
    }
    if kind is RunKind.QUESTION:
        assert set(servers) == {"jotflow"}  # no GitHub at all (#57)
        return
    assert set(servers) == {"jotflow", "github"}
    github = servers["github"]
    assert github["type"] == "local"
    assert github["command"] == "/usr/local/bin/github-mcp-server"
    assert ("--read-only" in github["args"]) is (kind is not RunKind.EXECUTION)
    assert github["args"][-2:] == ["--tools", ",".join(GITHUB_TOOLS[kind])]
    assert github["tools"] == list(GITHUB_TOOLS[kind])
    token = WRITE if kind is RunKind.EXECUTION else READ
    assert github["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": token}


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
def test_no_run_data_or_token_in_copilots_prompt_argv_or_env(kind: RunKind, tmp_path: Path) -> None:
    run = context(kind, tmp_path, model=MODEL)
    plan = COPILOT.plan(run)
    assert plan.argv[2] == COPILOT_PROMPTS[kind]
    joined = " ".join(plan.argv) + json.dumps(dict(plan.env))
    for secret in (run.mcp_token, run.github_token, run.run_id, run.mcp_url):
        assert secret not in joined
    assert plan.github_token_env is False


def test_copilots_state_stays_in_the_run_directory_and_it_runs_offline(tmp_path: Path) -> None:
    env = plan_for(RunKind.DRAFTING, tmp_path).env
    assert env == {
        "COPILOT_OFFLINE": "true",
        "COPILOT_HOME": str(tmp_path / "copilot"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    assert "--enable-memory" not in plan_for(RunKind.DRAFTING, tmp_path).argv


def test_copilot_needs_a_model(tmp_path: Path) -> None:
    assert COPILOT.default_models == {}
    with pytest.raises(ValueError, match="needs a model"):
        COPILOT.plan(context(RunKind.DRAFTING, tmp_path, model=None))
    with pytest.raises(ConfigError, match="needs a model; set JOTFLOW_AGENT_MODEL"):
        preset_config(preset=COPILOT)
    with pytest.raises(ConfigError, match="no model for question runs"):
        preset_config(preset=COPILOT, models={RunKind.DRAFTING: MODEL})
    preset_config(preset=COPILOT, models=dict.fromkeys((RunKind.DRAFTING, RunKind.QUESTION), MODEL))
    preset_config(
        preset=COPILOT,
        kinds=EXECUTION_KINDS,
        read_token=None,
        write_token=WRITE,
        models={RunKind.EXECUTION: MODEL},
    )


# The env allowlist (security's condition 1)


def test_the_four_provider_names_pass() -> None:
    assert COPILOT.check_env(ALLOW, PROVIDER) is None
    assert COPILOT.check_env((*ALLOW, "HTTPS_PROXY"), PROVIDER) is None
    assert COPILOT.check_env(ALLOW, {**PROVIDER, "COPILOT_PROVIDER_TYPE": "azure"}) is None


@pytest.mark.parametrize(
    "name",
    [
        "COPILOT_ALLOW_ALL",
        "COPILOT_PROVIDER_API_KEY_COMMAND",
        "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
        "COPILOT_HOME",
        "COPILOT_OFFLINE",
        "COPILOT_MODEL",
        "COPILOT_SOMETHING_A_LATER_RELEASE_ADDS",
        "XDG_CACHE_HOME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "GH_HOST",
    ],
)
def test_any_other_copilot_name_is_refused(name: str) -> None:
    problem = COPILOT.check_env((*ALLOW, name), {**PROVIDER, name: "x"})
    assert problem is not None and name in problem


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"COPILOT_PROVIDER_TYPE": "ollama"}, "must be openai, azure or anthropic"),
        ({"COPILOT_PROVIDER_TYPE": ""}, "needs COPILOT_PROVIDER_TYPE"),
        ({"COPILOT_PROVIDER_WIRE_API": "completions"}, "must be responses"),
        ({"COPILOT_PROVIDER_BASE_URL": "http://api.example.com/v1"}, "https://"),
        ({"COPILOT_PROVIDER_BASE_URL": "https://"}, "with a host"),
        ({"COPILOT_PROVIDER_BASE_URL": "https:///v1"}, "with a host"),
        ({"COPILOT_PROVIDER_BASE_URL": "https://user:pw@api.example.com/v1"}, "no login"),
        ({"COPILOT_PROVIDER_BASE_URL": "https://@evil.example/v1"}, "no login"),
        ({"COPILOT_PROVIDER_BASE_URL": "https://api.example.com/v1\nX: y"}, "no spaces or control"),
        ({"COPILOT_PROVIDER_BASE_URL": "https://api.exa\tmple.com/v1"}, "no spaces or control"),
        ({"COPILOT_PROVIDER_BASE_URL": " https://api.example.com/v1"}, "no spaces or control"),
        ({"COPILOT_PROVIDER_BASE_URL": "http://api.example.com/v1"}, "must be an https:// address"),
        (
            {"COPILOT_PROVIDER_BASE_URL": "HTTPS-ish://api.example.com"},
            "must be an https:// address",
        ),
        ({"COPILOT_PROVIDER_API_KEY": ""}, "needs COPILOT_PROVIDER_API_KEY"),
    ],
)
def test_the_provider_must_be_one_the_gate_checks(change: dict[str, str], problem: str) -> None:
    found = COPILOT.check_env(ALLOW, {**PROVIDER, **change})
    assert found is not None and problem in found


ANTHROPIC = {
    "COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
    "COPILOT_PROVIDER_TYPE": "anthropic",
    "COPILOT_PROVIDER_API_KEY": "sk-ant-test",
}
ANTHROPIC_ALLOW = tuple(n for n in ALLOW if n != "COPILOT_PROVIDER_WIRE_API")


def test_anthropic_passes_with_no_wire_setting() -> None:
    assert COPILOT.check_env(ANTHROPIC_ALLOW, ANTHROPIC) is None
    # Set but not passed on, it changes nothing: only listed names reach the run.
    unlisted = {**ANTHROPIC, "COPILOT_PROVIDER_WIRE_API": "x"}
    assert COPILOT.check_env(ANTHROPIC_ALLOW, unlisted) is None


@pytest.mark.parametrize("value", ["responses", "completions", ""])
def test_anthropic_refuses_a_listed_wire_setting(value: str) -> None:
    found = COPILOT.check_env(ALLOW, {**ANTHROPIC, "COPILOT_PROVIDER_WIRE_API": value})
    assert found == (
        "COPILOT_PROVIDER_WIRE_API must not be set with COPILOT_PROVIDER_TYPE=anthropic"
    )


def test_anthropic_still_needs_its_other_three_names_and_https() -> None:
    for name in ANTHROPIC_ALLOW:
        listed = tuple(n for n in ANTHROPIC_ALLOW if n != name)
        found = COPILOT.check_env(listed, ANTHROPIC)
        assert found is not None and name in found
    plain = {**ANTHROPIC, "COPILOT_PROVIDER_BASE_URL": "http://api.anthropic.com"}
    assert "https://" in str(COPILOT.check_env(ANTHROPIC_ALLOW, plain))


def test_every_provider_name_must_be_listed() -> None:
    for name in ALLOW:
        listed = tuple(n for n in ALLOW if n != name)
        found = COPILOT.check_env(listed, PROVIDER)
        assert found is not None and name in found


def runner_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for name in ("JOTFLOW_AGENT_COMMAND", "JOTFLOW_GITHUB_WRITE_TOKEN", *PROVIDER):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(presets.CopilotPreset, "check_install", lambda self, **kw: None)
    values = {
        "JOTFLOW_RUNNER_SECRET": SECRET,
        "JOTFLOW_AGENT_PRESET": "copilot",
        "JOTFLOW_AGENT_MODEL": MODEL,
        "JOTFLOW_GITHUB_READ_TOKEN": READ,
        "JOTFLOW_GITHUB_MCP_COMMAND": sys.executable,
        "JOTFLOW_AGENT_ENV_ALLOW": ",".join(ALLOW),
        **PROVIDER,
        **extra,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_the_runner_refuses_to_start_on_a_refused_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner_env(
        monkeypatch,
        JOTFLOW_AGENT_ENV_ALLOW=",".join((*ALLOW, "COPILOT_ALLOW_ALL")),
        COPILOT_ALLOW_ALL="true",
    )
    assert runner_command.run(runner_command.argparse.Namespace()) == runner_command.EXIT_CONFIG
    assert "COPILOT_ALLOW_ALL may not be passed to the copilot preset" in capsys.readouterr().err


def test_the_runner_refuses_to_start_on_an_unchecked_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner_env(monkeypatch, COPILOT_PROVIDER_TYPE="ollama")
    assert runner_command.run(runner_command.argparse.Namespace()) == runner_command.EXIT_CONFIG
    assert "must be openai, azure or anthropic" in capsys.readouterr().err


def test_a_good_copilot_runner_config_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    runner_env(monkeypatch)
    settings = runner_command.RunnerSettings()  # type: ignore[call-arg]
    config = runner_command.build_config(settings)
    assert config.preset is COPILOT and config.env_allow == ALLOW
    assert runner_command.startup_line(config).endswith("with preset copilot (experimental)")


def test_replay_makes_the_same_env_check() -> None:
    config = preset_config(
        preset=COPILOT,
        models=dict.fromkeys((RunKind.DRAFTING, RunKind.QUESTION), MODEL),
        env_allow=ALLOW,
    )
    replay_check_env(COPILOT, config, PROVIDER)
    with pytest.raises(ReplayError, match="must be openai, azure or anthropic"):
        replay_check_env(COPILOT, config, {**PROVIDER, "COPILOT_PROVIDER_TYPE": "ollama"})


# The installed CLI


class Done:
    def __init__(self, stdout: str, code: int = 0) -> None:
        self.stdout, self.returncode = stdout, code


def test_check_install_names_the_package_when_copilot_is_missing() -> None:
    assert COPILOT.check_install(which=lambda _: None) == (
        f"the copilot CLI is not installed; add @github/copilot@{COPILOT_VERSION} "
        "to AGENT_NPM_PACKAGES"
    )


def test_check_install_accepts_only_the_pinned_version() -> None:
    seen: list[dict[str, Any]] = []

    def ok(argv: list[str], **kw: Any) -> Done:
        seen.append(kw)
        return Done(f"GitHub Copilot CLI {COPILOT_VERSION}.\nRun 'copilot update' to check.\n")

    assert COPILOT.check_install(which=lambda n: f"/usr/bin/{n}", run=ok) is None
    # A scratch home, so the check unpacks nothing into the service's, and offline.
    assert seen[0]["env"]["COPILOT_OFFLINE"] == "true"
    assert seen[0]["env"]["HOME"] != os.environ.get("HOME")
    other = COPILOT.check_install(
        which=lambda n: f"/usr/bin/{n}", run=lambda *a, **k: Done("GitHub Copilot CLI 1.0.102.")
    )
    assert other == (
        f"the copilot preset needs Copilot CLI {COPILOT_VERSION}; this image has 1.0.102"
    )
    failed = COPILOT.check_install(which=lambda n: f"/usr/bin/{n}", run=lambda *a, **k: Done("", 1))
    assert failed is not None and "this image has unknown" in failed

    def boom(*a: Any, **k: Any) -> Done:
        raise subprocess.TimeoutExpired("copilot", 60)

    raised = COPILOT.check_install(which=lambda n: f"/usr/bin/{n}", run=boom)
    assert raised == "the copilot CLI could not be run (TimeoutExpired)"


# A whole run, with a stand-in CLI that records what it was given

FAKE_COPILOT = """#!{python}
import json, os, stat, sys
argv = sys.argv[1:]
config = argv[argv.index("--additional-mcp-config") + 1][1:]
record = {{
    "argv": argv,
    "env": dict(os.environ),
    "config_mode": stat.S_IMODE(os.stat(config).st_mode),
    "config": open(config).read(),
    "run_dir": os.path.dirname(config),
}}
with open(os.path.join(os.path.dirname(os.path.dirname(config)), "out.json"), "w") as handle:
    json.dump(record, handle)
"""


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
async def test_a_copilot_run_end_to_end(kind: RunKind, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    copilot = bin_dir / "copilot"
    copilot.write_text(FAKE_COPILOT.format(python=sys.executable), encoding="utf-8")
    copilot.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    execution = kind is RunKind.EXECUTION
    config = preset_config(
        preset=COPILOT,
        workdir=work,
        env_allow=ALLOW,
        kinds=EXECUTION_KINDS if execution else frozenset({RunKind.DRAFTING, RunKind.QUESTION}),
        read_token=None if execution else READ,
        write_token=WRITE if execution else None,
        models=(
            {RunKind.EXECUTION: MODEL}
            if execution
            else dict.fromkeys((RunKind.DRAFTING, RunKind.QUESTION), MODEL)
        ),
    )
    environ = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": "/home/agent",
        "COPILOT_ALLOW_ALL": "true",  # never passed on: it isn't in the allowlist
        "GITHUB_TOKEN": "ghp_never",
        **PROVIDER,
    }
    runner = Runner(config, environ=environ)
    await runner.launch(LaunchRequest(run_id="run-1", kind=kind, token=TOKEN))
    view = await finished(runner, "run-1")
    assert view["state"] == RunState.EXITED.value and view["exit_code"] == 0
    record = json.loads((work / "out.json").read_text(encoding="utf-8"))
    assert record["config_mode"] == 0o600
    assert f"Bearer {TOKEN}" in record["config"]
    env = record["env"]
    run_dir = Path(record["run_dir"])
    assert env["COPILOT_HOME"] == str(run_dir / "copilot")
    assert env["COPILOT_OFFLINE"] == "true"
    for name in PROVIDER:
        assert env[name] == PROVIDER[name]
    for name in ("COPILOT_ALLOW_ALL", "GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        assert name not in env
    github = WRITE if execution else READ
    for secret in (TOKEN, github):
        assert secret not in json.dumps(env) and secret not in " ".join(record["argv"])
    assert not run_dir.exists()  # the session, logs and caches go with the run
    assert stat.S_ISDIR(work.stat().st_mode)


# The gate's provider-side checks, which must fire on what strace can't see (#288)

CLIENT_TOOL = {"name": "jotflow-get_work", "description": "d", "input_schema": {"type": "object"}}


@pytest.mark.parametrize(
    "request_body",
    [
        {"tools": [CLIENT_TOOL, {"type": "web_search_20250305", "name": "web_search"}]},
        {"tools": [{"type": "code_execution_20250522", "name": "code_execution"}]},
        {"tools": [CLIENT_TOOL], "mcp_servers": [{"type": "url", "url": "https://x"}]},
        {"tools": [CLIENT_TOOL], "container": "container_1"},
        {"tools": [{"name": "no-schema"}]},
    ],
)
def test_the_gate_refuses_a_tool_anthropic_would_run_itself(request_body: dict[str, Any]) -> None:
    from jotflow.testing.fake_messages_api import FakeMessagesApi
    from tests.test_copilot_gate import assert_no_provider_side_tool

    api = FakeMessagesApi()
    api.requests.append(request_body)
    api.headers.append({})
    with pytest.raises(AssertionError):
        assert_no_provider_side_tool(api)
    api._server.server_close()


def test_the_gate_refuses_a_beta_nobody_reviewed() -> None:
    from jotflow.testing.fake_messages_api import FakeMessagesApi
    from tests.test_copilot_gate import REVIEWED_BETAS, assert_no_provider_side_tool

    api = FakeMessagesApi()
    api.requests.append({"tools": [CLIENT_TOOL]})
    api.headers.append({"anthropic-beta": ",".join(REVIEWED_BETAS)})
    assert_no_provider_side_tool(api)  # the reviewed ones pass
    api.headers.append({"anthropic-beta": "mcp-client-2025-04-04"})
    with pytest.raises(AssertionError, match="mcp-client"):
        assert_no_provider_side_tool(api)
    api._server.server_close()


@pytest.mark.parametrize("hosted", ["web_search", "file_search", "code_interpreter", "mcp"])
def test_the_gate_refuses_a_hosted_tool_on_the_responses_wire(hosted: str) -> None:
    from jotflow.testing.fake_responses_api import FakeResponsesApi
    from tests.test_copilot_gate import assert_no_provider_side_tool

    api = FakeResponsesApi()
    api.requests.append({"tools": [{"type": "function", "name": "t"}, {"type": hosted}]})
    with pytest.raises(AssertionError, match=hosted):
        assert_no_provider_side_tool(api)
    api._server.server_close()
