"""Agent presets (#22): what each run kind's agent may load and call."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from jotflow.adapters import presets
from jotflow.adapters.presets import (
    CLAUDE_CODE,
    GITHUB_TOOLS,
    JOTFLOW_TOOLS,
    PRESETS,
    CommandPreset,
    RunContext,
)
from jotflow.adapters.runner import (
    _FORBIDDEN_ENV,
    CONVERSATION_KINDS,
    EXECUTION_KINDS,
    ConfigError,
    LaunchRequest,
    Runner,
    RunnerConfig,
    RunState,
    parse_preset,
)
from jotflow.commands import runner as runner_command
from jotflow.models import PARTIAL_PRESET_KINDS, RUN_KINDS, RunKind, expected_runner_preset
from jotflow.store import MemoryStore
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.write_tools import write_tools
from tests.test_write_tools import POLICY, Poster

SECRET = "s" * 40
READ, WRITE = "github_pat_read0123456789", "github_pat_write0123456789"
TOKEN = "jfr_" + "t" * 43
SRC = Path(presets.__file__).resolve().parents[1]


def context(kind: RunKind, tmp_path: Path, model: str | None = "sonnet") -> RunContext:
    return RunContext(
        kind=kind,
        run_id="run-1",
        run_dir=tmp_path,
        config_path=tmp_path / "mcp.json",
        mcp_url="http://jotflow:8765/mcp",
        mcp_token=TOKEN,
        github_token=WRITE if kind is RunKind.EXECUTION else READ,
        model=model,
        github_mcp="/usr/local/bin/github-mcp-server",
    )


def test_presets_are_found_by_name() -> None:
    assert parse_preset("claude-code") is CLAUDE_CODE
    assert parse_preset(" claude-code ") is CLAUDE_CODE
    with pytest.raises(ConfigError, match="must be one of: claude-code"):
        parse_preset("claude")


def test_claude_code_drafting_argv_is_locked_down(tmp_path: Path) -> None:
    plan = CLAUDE_CODE.plan(context(RunKind.DRAFTING, tmp_path))
    allowed = (
        "mcp__jotflow__get_work,mcp__jotflow__list_topics,mcp__jotflow__get_topic,"
        "mcp__jotflow__propose_issue,mcp__jotflow__propose_update,"
        "mcp__jotflow__link_message,mcp__jotflow__ask_channel,"
        "mcp__github__issue_read,mcp__github__list_issues,mcp__github__search_issues,"
        "mcp__github__list_label,mcp__github__get_label"
    )
    assert plan.argv == (
        "claude",
        "-p",
        presets.CLAUDE_PROMPTS[RunKind.DRAFTING],
        "--mcp-config",
        str(tmp_path / "mcp.json"),
        "--strict-mcp-config",
        "--tools",
        "",
        "--allowedTools",
        allowed,
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--max-turns",
        "40",
        "--model",
        "sonnet",
    )


def test_no_model_leaves_the_cli_default(tmp_path: Path) -> None:
    plan = CLAUDE_CODE.plan(context(RunKind.EXECUTION, tmp_path, model=None))
    assert "--model" not in plan.argv


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
def test_claude_code_allows_exactly_the_kinds_tools(kind: RunKind, tmp_path: Path) -> None:
    plan = CLAUDE_CODE.plan(context(kind, tmp_path))
    allowed = plan.argv[plan.argv.index("--allowedTools") + 1].split(",")
    assert allowed == [f"mcp__jotflow__{t}" for t in JOTFLOW_TOOLS[kind]] + [
        f"mcp__github__{t}" for t in GITHUB_TOOLS[kind]
    ]
    # Never a built-in tool, and never a wildcard.
    assert all(name.startswith(("mcp__jotflow__", "mcp__github__")) for name in allowed)
    assert not any("*" in name for name in allowed)
    # No untrusted text in argv: the prompt is one of the preset's fixed strings.
    assert plan.argv[2] == presets.CLAUDE_PROMPTS[kind]


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
def test_claude_code_loads_only_jotflow_and_the_pinned_github_server(
    kind: RunKind, tmp_path: Path
) -> None:
    plan = CLAUDE_CODE.plan(context(kind, tmp_path))
    servers = plan.config["mcpServers"]
    assert set(servers) == {"jotflow", "github"}
    assert servers["jotflow"] == {
        "type": "http",
        "url": "http://jotflow:8765/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }
    github = servers["github"]
    assert github["type"] == "stdio"
    assert github["command"] == "/usr/local/bin/github-mcp-server"
    read_only = kind is not RunKind.EXECUTION
    assert ("--read-only" in github["args"]) is read_only
    assert github["args"][0] == "stdio"
    assert github["args"][-2:] == ["--tools", ",".join(GITHUB_TOOLS[kind])]
    assert github["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": WRITE if not read_only else READ}
    # The GitHub token reaches only the GitHub server, never the CLI's own env.
    assert plan.github_token_env is False
    assert TOKEN not in " ".join(plan.argv)


def test_only_execution_can_write_to_github() -> None:
    writes = {"issue_write", "add_issue_comment"}
    assert writes <= set(GITHUB_TOOLS[RunKind.EXECUTION])
    for kind in (RunKind.DRAFTING, RunKind.QUESTION):
        assert not writes & set(GITHUB_TOOLS[kind])
    assert set(JOTFLOW_TOOLS[RunKind.EXECUTION]) == {"get_work", "record_result"}


def test_the_jotflow_tool_table_matches_the_whiteboard() -> None:
    store = MemoryStore()
    specs = (*read_tools(store, POLICY), *write_tools(store, POLICY, Poster()))
    for kind in sorted(RUN_KINDS):
        declared = {spec.name for spec in specs if kind in spec.kinds}
        assert set(JOTFLOW_TOOLS[kind]) == declared, kind


@pytest.mark.parametrize("kind", sorted(RUN_KINDS))
def test_claude_code_keeps_its_logs_in_the_run_directory(kind: RunKind, tmp_path: Path) -> None:
    env = CLAUDE_CODE.plan(context(kind, tmp_path)).env
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert env["DISABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_UPDATES"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_every_kind_has_a_turn_limit(tmp_path: Path) -> None:
    for kind in sorted(RUN_KINDS):
        argv = CLAUDE_CODE.plan(context(kind, tmp_path)).argv
        assert int(argv[argv.index("--max-turns") + 1]) == presets.CLAUDE_MAX_TURNS[kind] > 0


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_preset_env_never_touches_the_runners_names(name: str, tmp_path: Path) -> None:
    served = getattr(PRESETS[name], "run_kinds", RUN_KINDS)
    for kind in sorted(served):
        for key in PRESETS[name].plan(context(kind, tmp_path)).env:
            assert not _FORBIDDEN_ENV.search(key)
            assert key not in {"HOME", "TMPDIR", "PATH"}


def test_the_command_preset_keeps_54s_behavior(tmp_path: Path) -> None:
    plan = CommandPreset(("agent", "--config={mcp_config}", "{run_id}")).plan(
        context(RunKind.DRAFTING, tmp_path)
    )
    assert plan.argv == ("agent", f"--config={tmp_path / 'mcp.json'}", "run-1")
    assert set(plan.config["mcpServers"]) == {"jotflow"}
    assert plan.github_token_env is True
    assert plan.env == {}


def test_the_orchestrator_and_whiteboard_never_import_presets() -> None:
    for package in ("orchestrator", "whiteboard"):
        for path in (SRC / package).rglob("*.py"):
            assert "jotflow.adapters" not in path.read_text(encoding="utf-8"), path


def test_the_orchestrator_knows_each_presets_run_kinds() -> None:
    # The orchestrator checks runners against PARTIAL_PRESET_KINDS, not the presets.
    for name, preset in PRESETS.items():
        kinds = getattr(preset, "run_kinds", RUN_KINDS)
        assert PARTIAL_PRESET_KINDS.get(name, RUN_KINDS) == kinds, name
    assert set(PARTIAL_PRESET_KINDS) <= set(PRESETS)
    with pytest.raises(TypeError):
        PARTIAL_PRESET_KINDS["command"] = RUN_KINDS  # type: ignore[index]


def test_only_a_known_preset_without_execution_hands_it_to_claude_code() -> None:
    execution = frozenset({RunKind.EXECUTION})
    conversation = frozenset({RunKind.DRAFTING, RunKind.QUESTION})
    assert expected_runner_preset(execution, "codex") == "claude-code"
    assert expected_runner_preset(conversation, "codex") == "codex"
    assert expected_runner_preset(RUN_KINDS, "codex") == "codex"
    for preset in ("claude-code", "command", "unknown"):
        assert expected_runner_preset(execution, preset) == preset
        assert expected_runner_preset(conversation, preset) == preset


def preset_config(**overrides: Any) -> RunnerConfig:
    fields: dict[str, Any] = {
        "secret": SECRET,
        "mcp_url": "http://jotflow:8765/mcp",
        "preset": CLAUDE_CODE,
        "read_token": READ,
    }
    fields.update(overrides)
    return RunnerConfig(**fields)


def test_models_default_per_kind_and_can_be_overridden() -> None:
    config = preset_config()
    assert config.model(RunKind.DRAFTING) == "claude-sonnet-5-5"
    assert config.model(RunKind.QUESTION) == "claude-sonnet-5-5"
    config = preset_config(models={RunKind.DRAFTING: "claude-sonnet-5-5[1m]"})
    assert config.model(RunKind.DRAFTING) == "claude-sonnet-5-5[1m]"
    assert config.model(RunKind.QUESTION) == "claude-sonnet-5-5"
    execution = preset_config(kinds=EXECUTION_KINDS, read_token=None, write_token=WRITE)
    assert execution.model(RunKind.EXECUTION) == "claude-haiku-5-5"


def test_claude_code_defaults_are_full_model_ids_not_aliases() -> None:
    # An alias resolves per CLI version: at 2.1.303, "haiku" picks Haiku 4.5.
    for model in CLAUDE_CODE.default_models.values():
        assert model.startswith("claude-"), model


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"command": ("claude",)}, "exactly one of JOTFLOW_AGENT_PRESET"),
        ({"preset": None}, "exactly one of JOTFLOW_AGENT_PRESET"),
        (
            {"preset": None, "command": ("x",), "models": {RunKind.DRAFTING: "haiku"}},
            "need a preset",
        ),
        ({"models": {RunKind.EXECUTION: "haiku"}}, "does not serve execution"),
        ({"models": {RunKind.DRAFTING: "haiku; rm -rf /"}}, "not a model name"),
        ({"models": {RunKind.DRAFTING: "--dangerously"}}, "not a model name"),
        ({"read_token": "ghp_${HOME}"}, "characters a GitHub token never has"),
        ({"read_token": "ghp_abc def"}, "characters a GitHub token never has"),
    ],
)
def test_bad_preset_configuration_fails_at_startup(overrides: dict[str, Any], problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        preset_config(**overrides)


# A stand-in for the claude CLI: records what it was given, then exits.
FAKE_CLAUDE = """#!{python}
import json, os, stat, sys
argv = sys.argv[1:]
config_path = argv[argv.index("--mcp-config") + 1]
run_dir = os.path.dirname(config_path)
record = {{
    "argv": argv,
    "env": dict(os.environ),
    "config": json.load(open(config_path)),
    "config_mode": stat.S_IMODE(os.stat(config_path).st_mode),
    "dir_mode": stat.S_IMODE(os.stat(run_dir).st_mode),
    "run_dir": run_dir,
}}
os.makedirs(os.environ["XDG_CACHE_HOME"])
open(os.path.join(os.environ["XDG_CACHE_HOME"], "mcp.log"), "w").write("log")
with open(os.path.join(os.path.dirname(run_dir), "out.json"), "w") as handle:
    json.dump(record, handle)
"""


async def finished(runner: Runner, run_id: str) -> dict[str, Any]:
    for _ in range(400):
        view = runner.get(run_id)
        if view is not None and view["state"] not in ("starting", "running"):
            return view
        await asyncio.sleep(0.025)
    raise AssertionError("the run did not finish")


@pytest.mark.parametrize("execution", [False, True])
async def test_a_claude_code_run_end_to_end(tmp_path: Path, execution: bool) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    claude.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    kind = RunKind.EXECUTION if execution else RunKind.DRAFTING
    config = preset_config(
        kinds=EXECUTION_KINDS if execution else CONVERSATION_KINDS,
        read_token=None if execution else READ,
        write_token=WRITE if execution else None,
        env_allow=("ANTHROPIC_API_KEY",),
        workdir=work,
        models={kind: "haiku"},
    )
    environ = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": "/home/agent",
        "ANTHROPIC_API_KEY": "model-key",
        "JOTFLOW_GITHUB_READ_TOKEN": READ,
    }
    runner = Runner(config, environ=environ)
    await runner.launch(LaunchRequest(run_id="run-1", kind=kind, token=TOKEN))
    view = await finished(runner, "run-1")
    assert view == {"run_id": "run-1", "state": RunState.EXITED.value, "exit_code": 0}

    record = json.loads((work / "out.json").read_text(encoding="utf-8"))
    assert record["argv"][record["argv"].index("--model") + 1] == "haiku"
    assert record["config_mode"] == 0o600
    assert record["dir_mode"] == 0o700
    github = record["config"]["mcpServers"]["github"]
    assert github["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == (WRITE if execution else READ)
    env = record["env"]
    # No GitHub token in the CLI's own environment with a preset, and no secrets.
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "JOTFLOW_GITHUB_READ_TOKEN"):
        assert name not in env
    assert READ not in json.dumps(env) and WRITE not in json.dumps(env)
    assert TOKEN not in json.dumps(env) and TOKEN not in " ".join(record["argv"])
    assert env["ANTHROPIC_API_KEY"] == "model-key"
    assert env["XDG_CACHE_HOME"] == str(Path(record["run_dir"]) / "cache")
    assert env["HOME"] == "/home/agent"
    assert env["JOTFLOW_RUN_KIND"] == kind.value
    # The config, the logs, and the whole run directory are gone afterwards.
    assert not Path(record["run_dir"]).exists()
    assert [p.name for p in work.iterdir()] == ["out.json"]


@pytest.fixture
def claude_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about settings, so they don't depend on the host's PATH."""
    monkeypatch.setattr(presets.ClaudeCodePreset, "check_install", lambda self, **kw: None)


def test_claude_code_needs_its_cli_installed() -> None:
    problem = CLAUDE_CODE.check_install(which=lambda _: None)
    assert problem == (
        "the claude CLI is not installed; add @anthropic-ai/claude-code@"
        f"{presets.CLAUDE_CODE_VERSION} to AGENT_NPM_PACKAGES"
    )
    assert CLAUDE_CODE.check_install(which=lambda name: f"/usr/bin/{name}") is None


def test_the_claude_code_pin_matches_docker_compose() -> None:
    root = SRC.parent.parent
    for compose in (root / "docker-compose.yml", SRC / "templates" / "docker-compose.yml"):
        pin = f"@anthropic-ai/claude-code@{presets.CLAUDE_CODE_VERSION}"
        assert pin in compose.read_text(encoding="utf-8"), compose


def test_a_claude_code_runner_without_the_cli_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = presets.ClaudeCodePreset.check_install
    monkeypatch.setattr(
        presets.ClaudeCodePreset, "check_install", lambda self: missing(self, which=lambda _: None)
    )
    monkeypatch.delenv("JOTFLOW_AGENT_COMMAND", raising=False)
    for name, value in {
        "JOTFLOW_RUNNER_SECRET": SECRET,
        "JOTFLOW_AGENT_PRESET": "claude-code",
        "JOTFLOW_GITHUB_READ_TOKEN": READ,
        "JOTFLOW_GITHUB_MCP_COMMAND": sys.executable,
    }.items():
        monkeypatch.setenv(name, value)
    assert runner_command.run(runner_command.argparse.Namespace()) == runner_command.EXIT_CONFIG
    assert "the claude CLI is not installed" in capsys.readouterr().err


@pytest.mark.usefixtures("claude_installed")
def test_settings_select_a_preset_and_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "github-mcp-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(stat.S_IRWXU)
    for name, value in {
        "JOTFLOW_RUNNER_SECRET": SECRET,
        "JOTFLOW_AGENT_PRESET": "claude-code",
        "JOTFLOW_GITHUB_READ_TOKEN": READ,
        "JOTFLOW_AGENT_MODEL": "haiku",
        "JOTFLOW_AGENT_MODEL_DRAFTING": "opus",
        "JOTFLOW_GITHUB_MCP_COMMAND": str(binary),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("JOTFLOW_AGENT_COMMAND", raising=False)
    config = runner_command.build_config(runner_command.RunnerSettings())  # type: ignore[call-arg]
    assert config.preset is CLAUDE_CODE
    assert config.command == ()
    assert config.models == {RunKind.DRAFTING: "opus", RunKind.QUESTION: "haiku"}
    assert config.github_mcp == str(binary)


@pytest.mark.usefixtures("claude_installed")
@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"JOTFLOW_AGENT_PRESET": "claude-code", "JOTFLOW_AGENT_COMMAND": '["x"]'}, "exactly one"),
        ({}, "exactly one"),
        (
            {"JOTFLOW_AGENT_PRESET": "claude-code", "JOTFLOW_GITHUB_MCP_COMMAND": "/nonexistent"},
            "GitHub MCP server is not installed at /nonexistent",
        ),
        ({"JOTFLOW_AGENT_PRESET": "nope"}, "must be one of"),
    ],
)
def test_runner_preset_settings_problems_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    problem: str,
) -> None:
    for name in ("JOTFLOW_AGENT_PRESET", "JOTFLOW_AGENT_COMMAND", "JOTFLOW_GITHUB_MCP_COMMAND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JOTFLOW_RUNNER_SECRET", SECRET)
    monkeypatch.setenv("JOTFLOW_GITHUB_READ_TOKEN", READ)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert runner_command.run(runner_command.argparse.Namespace()) == runner_command.EXIT_CONFIG
    assert problem in capsys.readouterr().err


def test_no_prompt_carries_run_data(tmp_path: Path) -> None:
    """D1: the prompt is fixed per kind. Run ids, tokens, paths, and models never reach it."""
    for kind in sorted(RUN_KINDS):
        run = context(kind, tmp_path, model="claude-sonnet-5-5")
        plan = CLAUDE_CODE.plan(run)
        prompt = plan.argv[plan.argv.index("-p") + 1]
        assert prompt == presets.CLAUDE_PROMPTS[kind]
        for value in (run.run_id, run.mcp_token, run.github_token, run.mcp_url, str(tmp_path)):
            assert value not in prompt
        assert "claude-sonnet" not in prompt
    # The same prompt for any two runs of one kind.
    first = CLAUDE_CODE.plan(context(RunKind.DRAFTING, tmp_path / "a"))
    second = CLAUDE_CODE.plan(
        RunContext(
            kind=RunKind.DRAFTING,
            run_id="run-2",
            run_dir=tmp_path / "b",
            config_path=tmp_path / "b" / "mcp.json",
            mcp_url="http://elsewhere:1/mcp",
            mcp_token="jfr_" + "u" * 43,
            github_token="github_pat_other",
            model="haiku",
        )
    )
    assert first.argv[2] == second.argv[2]


def test_preset_env_cannot_replace_base_allowlisted_or_runner_names(tmp_path: Path) -> None:
    from jotflow.adapters.runner import child_env

    config = preset_config(env_allow=("ANTHROPIC_API_KEY",))
    environ = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "model-key", "HOME": "/home/agent"}
    extra = {
        "PATH": "/evil",
        "NODE_OPTIONS": "--require /evil.js",  # a base name, even when the runner has none
        "ANTHROPIC_API_KEY": "other",
        "HOME": "/evil",
        "TMPDIR": "/evil",
        "JOTFLOW_RUN_ID": "other",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    env = child_env(config, RunKind.DRAFTING, "run-1", tmp_path, environ, extra=extra)
    assert env["PATH"] == "/usr/bin"
    assert "NODE_OPTIONS" not in env
    assert env["ANTHROPIC_API_KEY"] == "model-key"
    assert env["HOME"] == "/home/agent"
    assert env["TMPDIR"] == str(tmp_path)
    assert env["JOTFLOW_RUN_ID"] == "run-1"
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "cache")


# Codex CLI (experimental)


def codex_context(kind: RunKind, tmp_path: Path, model: str | None = None) -> RunContext:
    return context(kind, tmp_path, model=model)


def codex_config(kind: RunKind, tmp_path: Path, model: str | None = None) -> dict[str, Any]:
    import tomllib

    plan = presets.CODEX.plan(codex_context(kind, tmp_path, model))
    return tomllib.loads(plan.files["codex/config.toml"])


def test_codex_is_an_experimental_preset() -> None:
    assert parse_preset("codex") is presets.CODEX
    assert {"codex"} == presets.EXPERIMENTAL
    assert "claude-code" not in presets.EXPERIMENTAL


CODEX_KINDS = sorted(presets.CODEX_RUN_KINDS)


@pytest.mark.parametrize("kind", CODEX_KINDS)
def test_codex_argv_carries_no_run_data(kind: RunKind, tmp_path: Path) -> None:
    plan = presets.CODEX.plan(codex_context(kind, tmp_path, "gpt-5.5-codex"))
    assert plan.argv == (
        "codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--color",
        "never",
        presets.CLAUDE_PROMPTS[kind],
    )
    joined = " ".join(plan.argv)
    for value in (TOKEN, READ, WRITE, "gpt-5.5-codex", str(tmp_path), "run-1"):
        assert value not in joined
    assert plan.env == {"CODEX_HOME": str(tmp_path / "codex")}
    assert plan.github_token_env is False
    assert plan.config == {"mcpServers": {}}  # Codex reads only its own config


@pytest.mark.parametrize("kind", CODEX_KINDS)
def test_codex_config_is_pinned(kind: RunKind, tmp_path: Path) -> None:
    config = codex_config(kind, tmp_path, "gpt-5.5-codex")
    assert config["model"] == "gpt-5.5-codex"
    assert (config["approval_policy"], config["sandbox_mode"], config["web_search"]) == (
        "never",
        "read-only",
        "disabled",
    )
    assert config["check_for_update_on_startup"] is False
    assert config["history"] == {"persistence": "none"}
    assert config["analytics"] == {"enabled": False} and config["feedback"] == {"enabled": False}
    features = config["features"]
    assert features.pop("mcp_2026_07_28") is True
    assert features == dict.fromkeys(presets.CODEX_FEATURES_OFF, False)
    assert {"shell_tool", "unified_exec"} <= set(presets.CODEX_FEATURES_OFF)
    assert config["tools"] == {"experimental_request_user_input": {"enabled": False}}
    servers = config["mcp_servers"]
    assert set(servers) == {"jotflow", "github"}
    jotflow, github = servers["jotflow"], servers["github"]
    assert jotflow == {
        "url": "http://jotflow:8765/mcp",
        "http_headers": {"Authorization": f"Bearer {TOKEN}"},
        "enabled_tools": list(JOTFLOW_TOOLS[kind]),
        "default_tools_approval_mode": "approve",
        "required": True,
    }
    assert github["command"] == "/usr/local/bin/github-mcp-server"
    assert "--read-only" in github["args"]  # Codex serves conversation runs only
    assert github["enabled_tools"] == list(GITHUB_TOOLS[kind])
    assert github["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": READ}


def test_codex_defaults_to_its_verified_model_and_refuses_others() -> None:
    config = preset_config(preset=presets.CODEX)
    assert {config.model(k) for k in presets.CODEX_RUN_KINDS} == {"gpt-5.5"}
    assert preset_config(preset=presets.CODEX, models={RunKind.DRAFTING: "gpt-5.5"})
    with pytest.raises(ConfigError, match="accepts only these models"):
        preset_config(preset=presets.CODEX, models={RunKind.DRAFTING: "gpt-6.1-sol"})


def test_codex_without_a_model_leaves_it_out(tmp_path: Path) -> None:
    assert "model" not in codex_config(RunKind.DRAFTING, tmp_path)


def test_the_toml_writer_escapes_and_refuses_what_it_cannot_write() -> None:
    import tomllib

    text = presets.toml({"a": 'quote " and \\ back', "t": {"n": 3, "l": [1, "x"]}})
    assert tomllib.loads(text) == {"a": 'quote " and \\ back', "t": {"n": 3, "l": [1, "x"]}}
    with pytest.raises(TypeError):
        presets.toml({"bad": 1.5})


async def test_a_codex_run_writes_its_private_home(tmp_path: Path) -> None:
    from jotflow.adapters.runner import write_private

    plan = presets.CODEX.plan(codex_context(RunKind.DRAFTING, tmp_path))
    [(relative, text)] = plan.files.items()
    target = write_private(tmp_path, relative, text)
    assert target == tmp_path / "codex" / "config.toml"
    assert stat.S_IMODE((tmp_path / "codex").stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    for bad in ("../escape", "/abs", "a/../b", ""):
        with pytest.raises(ConfigError, match="inside the run directory"):
            write_private(tmp_path, bad, "x")


def test_the_startup_line_marks_an_experimental_preset() -> None:
    config = preset_config(preset=presets.CODEX)
    assert runner_command.startup_line(config) == (
        "runner serving drafting, question with preset codex (experimental)"
    )


FAKE_CODEX = """#!{python}
import json, os, stat, sys
home = os.environ["CODEX_HOME"]
config = os.path.join(home, "config.toml")
record = {{
    "argv": sys.argv[1:],
    "env": dict(os.environ),
    "config_mode": stat.S_IMODE(os.stat(config).st_mode),
    "home_mode": stat.S_IMODE(os.stat(home).st_mode),
    "config": open(config).read(),
    "run_dir": os.path.dirname(home),
}}
with open(os.path.join(os.path.dirname(os.path.dirname(home)), "out.json"), "w") as handle:
    json.dump(record, handle)
"""


async def test_a_codex_run_end_to_end(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(FAKE_CODEX.format(python=sys.executable), encoding="utf-8")
    codex.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    config = preset_config(preset=presets.CODEX, workdir=work, env_allow=("OPENAI_API_KEY",))
    environ = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": "/home/agent",
        "OPENAI_API_KEY": "k",
    }
    runner = Runner(config, environ=environ)
    await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    view = await finished(runner, "run-1")
    assert view["state"] == RunState.EXITED.value and view["exit_code"] == 0
    record = json.loads((work / "out.json").read_text(encoding="utf-8"))
    assert (record["home_mode"], record["config_mode"]) == (0o700, 0o600)
    assert f"Bearer {TOKEN}" in record["config"] and READ in record["config"]
    env = record["env"]
    assert env["CODEX_HOME"] == str(Path(record["run_dir"]) / "codex")
    assert env["OPENAI_API_KEY"] == "k"
    for secret in (TOKEN, READ):
        assert secret not in json.dumps(env) and secret not in " ".join(record["argv"])
    assert not Path(record["run_dir"]).exists()  # the home, config, and logs go with the run


def test_codex_never_serves_execution_runs(tmp_path: Path) -> None:
    # Codex keeps its MCP resource tools, so a run holding the write token could pull
    # arbitrary repository text into its context. Execution stays with claude-code.
    assert {RunKind.DRAFTING, RunKind.QUESTION} == presets.CODEX_RUN_KINDS
    with pytest.raises(ConfigError, match="may not serve execution runs; use claude-code"):
        preset_config(
            preset=presets.CODEX, kinds=EXECUTION_KINDS, read_token=None, write_token=WRITE
        )
    with pytest.raises(ValueError, match="does not serve execution"):
        presets.CODEX.plan(context(RunKind.EXECUTION, tmp_path))


def test_an_execution_runner_set_to_codex_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("JOTFLOW_AGENT_COMMAND", "JOTFLOW_GITHUB_READ_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(presets.CodexPreset, "check_install", lambda self, **kw: None)
    for name, value in {
        "JOTFLOW_RUNNER_SECRET": SECRET,
        "JOTFLOW_AGENT_PRESET": "codex",
        "JOTFLOW_GITHUB_WRITE_TOKEN": WRITE,
        "JOTFLOW_RUNNER_KINDS": "execution",
        "JOTFLOW_GITHUB_MCP_COMMAND": sys.executable,
    }.items():
        monkeypatch.setenv(name, value)
    assert runner_command.run(runner_command.argparse.Namespace()) == runner_command.EXIT_CONFIG
    assert "may not serve execution runs; use claude-code" in capsys.readouterr().err


def test_private_files_never_follow_a_symlink(tmp_path: Path) -> None:
    from jotflow.adapters.runner import write_private

    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "link").symlink_to(outside)
    with pytest.raises(ConfigError, match="directory is a symlink"):
        write_private(run_dir, "link/config.toml", "x")
    (run_dir / "file.toml").symlink_to(outside / "target")
    with pytest.raises(OSError):
        write_private(run_dir, "file.toml", "x")
    assert list(outside.iterdir()) == []
