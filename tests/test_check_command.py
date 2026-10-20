import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from jotflow.cli import main
from jotflow.commands import check as check_command
from jotflow.commands.init import launch_command
from jotflow.policy import parse_policy

# The commands check suggests, as this test run was started: uv run jotflow in a checkout.
JOTFLOW = launch_command()

ONE_CHANNEL = """
[channels.C0TEST1]

[[channels.C0TEST1.repos]]
repo = "acme/web"
nickname = "web"
description = "The web app"
"""

TWO_CHANNELS = (
    ONE_CHANNEL
    + """
[channels.C0TEST2]

[[channels.C0TEST2.repos]]
repo = "acme/api"
nickname = "api"
description = "The API"
"""
)


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("JOTFLOW_") or name == "CI":
            monkeypatch.delenv(name)
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    (tmp_path / "jotflow.toml").write_text(ONE_CHANNEL, encoding="utf-8")
    return tmp_path


@pytest.fixture
def no_slack(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Fail if the live check would reach Slack; record what it would have run."""
    runs: list[tuple[str, float]] = []

    def fake_make_slack(settings: Any) -> str:
        return "fake-slack"

    async def fake_run(slack: Any, channel: str, *, out: Any, timeout: float) -> int:
        assert slack == "fake-slack"
        runs.append((channel, timeout))
        return 0

    monkeypatch.setattr(check_command, "make_slack", fake_make_slack)
    monkeypatch.setattr(check_command, "run_live_check", fake_run)
    return runs


def live_args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"live": True, "channel": None, "timeout": 30.0, "yes": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_offline_check_reports_configuration(
    workspace: Path, no_slack: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check"]) == 0
    assert "ok    configuration: 1 channel(s) in jotflow.toml" in capsys.readouterr().out
    assert no_slack == []


def test_offline_check_reports_configuration_problems(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "jotflow.toml").write_text("issue_link = 'none'\n" + ONE_CHANNEL)
    assert main(["check"]) == 2
    assert "issue_link: unknown key" in capsys.readouterr().err


def test_live_check_needs_explicit_opt_in(
    workspace: Path, no_slack: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_command.run(live_args()) == 2
    assert f"JOTFLOW_LIVE_CHECK=1 {JOTFLOW} check --live" in capsys.readouterr().err
    assert no_slack == []


def test_opt_in_in_dotenv_is_not_enough(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace / ".env").write_text("JOTFLOW_LIVE_CHECK=1\n", encoding="utf-8")
    assert check_command.run(live_args()) == 2
    assert no_slack == []


def test_opt_in_left_in_dotenv_is_refused_even_with_the_variable_set(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    (workspace / ".env").write_text("JOTFLOW_LIVE_CHECK=1\n", encoding="utf-8")
    assert check_command.run(live_args()) == 2
    err = capsys.readouterr().err
    assert "remove the JOTFLOW_LIVE_CHECK line from .env and set it only for this one run" in err
    assert no_slack == []


class FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_terminal_run_asks_and_a_no_posts_nothing(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert check_command.run(live_args()) == 2
    assert "not posting; nothing was sent" in capsys.readouterr().err
    assert no_slack == []


def test_terminal_run_posts_after_a_yes(
    workspace: Path, no_slack: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    assert check_command.run(live_args()) == 0
    assert prompts == ["Post one test message to channel C0TEST1? [y/N] "]
    assert no_slack == [("C0TEST1", 30.0)]


def test_yes_flag_skips_the_question(
    workspace: Path, no_slack: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", pytest.fail)
    assert check_command.run(live_args(yes=True)) == 0
    assert no_slack == [("C0TEST1", 30.0)]


def test_confirm_rules() -> None:
    assert check_command.confirm("C1", interactive=False, ask=pytest.fail)
    assert check_command.confirm("C1", interactive=True, ask=lambda p: " YES ")
    assert not check_command.confirm("C1", interactive=True, ask=lambda p: "")

    def closed(prompt: str) -> str:
        raise EOFError

    assert not check_command.confirm("C1", interactive=True, ask=closed)


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_live_check_refuses_in_ci(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    monkeypatch.setenv("CI", value)
    assert check_command.run(live_args()) == 2
    assert "never runs in CI" in capsys.readouterr().err
    assert no_slack == []


def test_ci_false_does_not_block(
    workspace: Path, no_slack: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    monkeypatch.setenv("CI", "false")
    assert check_command.run(live_args()) == 0
    assert no_slack == [("C0TEST1", 30.0)]


def test_live_check_names_the_channel_before_posting(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    assert check_command.run(live_args(timeout=5.0)) == 0
    out = capsys.readouterr().out
    assert "posting one test message to channel C0TEST1, then deleting it" in out
    assert "Stop any running JotFlow first" in out
    assert no_slack == [("C0TEST1", 5.0)]


def test_live_check_rejects_a_non_positive_timeout(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    assert check_command.run(live_args(timeout=0.0)) == 2
    assert "--timeout" in capsys.readouterr().err
    assert no_slack == []


def test_choose_channel_rules() -> None:
    one = parse_policy(ONE_CHANNEL)
    two = parse_policy(TWO_CHANNELS)
    assert check_command.choose_channel(None, {}, one) == "C0TEST1"
    assert check_command.choose_channel("C0TEST2", {}, two) == "C0TEST2"
    env = {check_command.CHANNEL_VAR: "C0TEST2"}
    assert check_command.choose_channel(None, env, two) == "C0TEST2"
    assert check_command.choose_channel("C0TEST1", env, two) == "C0TEST1"
    with pytest.raises(check_command.Refusal, match="choose one with --channel"):
        check_command.choose_channel(None, {}, two)
    with pytest.raises(check_command.Refusal, match=r"not in jotflow\.toml"):
        check_command.choose_channel("C0ELSEWHERE", {}, one)


def test_make_slack_builds_the_real_adapter(workspace: Path) -> None:
    from jotflow.commands.run import load_settings
    from jotflow.live_check import BoltLiveSlack

    assert isinstance(check_command.make_slack(load_settings()), BoltLiveSlack)


def test_live_check_refuses_inside_the_jotflow_image(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_IMAGE", "1")
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    assert check_command.run(live_args()) == 2
    err = capsys.readouterr().err
    assert "run the live check on the host: JOTFLOW_LIVE_CHECK=1 make live-check" in err
    assert no_slack == []


def test_offline_check_still_works_inside_the_image(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_IMAGE", "1")
    assert main(["check"]) == 0
    assert "ok    configuration" in capsys.readouterr().out


def test_ctrl_c_at_the_prompt_cancels_cleanly(
    workspace: Path,
    no_slack: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))

    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert check_command.run(live_args()) == check_command.EXIT_CANCELLED
    assert "cancelled, nothing posted" in capsys.readouterr().err
    assert no_slack == []


@pytest.mark.parametrize("value", ["", "0", "false", "No"])
def test_a_plainly_false_image_marker_does_not_refuse(
    workspace: Path, no_slack: list[Any], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("JOTFLOW_IMAGE", value)
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    assert check_command.run(live_args(yes=True)) == 0
    assert no_slack == [("C0TEST1", 30.0)]


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
def test_any_other_image_marker_refuses(
    workspace: Path, no_slack: list[Any], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("JOTFLOW_IMAGE", value)
    monkeypatch.setenv("JOTFLOW_LIVE_CHECK", "1")
    assert check_command.run(live_args(yes=True)) == 2
    assert no_slack == []


# The agent's readiness (#41)

SECRET_A = "a" * 40
SECRET_B = "b" * 40
READ_TOKEN = "github_pat_readSENTINEL"
WRITE_TOKEN = "github_pat_writeSENTINEL"
MODEL_KEY = "sk-ant-SENTINEL"


def _agent_folder(folder: Path, *, profiles: str = "agent", **blank: bool) -> None:
    def value(key: str, real: str) -> str:
        return "" if blank.get(key) else real

    (folder / ".env").write_text(
        f"JOTFLOW_RUNNER_SECRET={value('env_secret', SECRET_A)}\n"
        f"JOTFLOW_RUNNER_EXEC_SECRET={SECRET_B}\nCOMPOSE_PROFILES={profiles}\n",
        encoding="utf-8",
    )
    (folder / "agent.env").write_text(
        f"JOTFLOW_RUNNER_SECRET={SECRET_A}\n"
        f"JOTFLOW_GITHUB_READ_TOKEN={value('read', READ_TOKEN)}\n"
        "JOTFLOW_AGENT_PRESET=claude-code\n"
        f"ANTHROPIC_API_KEY={value('model', MODEL_KEY)}\n"
        "JOTFLOW_AGENT_ENV_ALLOW=ANTHROPIC_API_KEY\n",
        encoding="utf-8",
    )
    (folder / "agent-exec.env").write_text(
        f"JOTFLOW_RUNNER_SECRET={SECRET_B}\n"
        f"JOTFLOW_GITHUB_WRITE_TOKEN={value('write', WRITE_TOKEN)}\n"
        f"JOTFLOW_AGENT_PRESET={'claude-code' if not blank.get('exec_preset') else 'codex'}\n"
        f"ANTHROPIC_API_KEY={MODEL_KEY}\nJOTFLOW_AGENT_ENV_ALLOW=ANTHROPIC_API_KEY\n",
        encoding="utf-8",
    )


def _with_preset(folder: Path, preset: str = "claude-code") -> None:
    (folder / "jotflow.toml").write_text(f'[agent]\npreset = "{preset}"\n' + ONE_CHANNEL)


def test_no_preset_means_no_agent_lines(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check"]) == 0
    assert "agent" not in capsys.readouterr().out


def test_a_ready_agent_setup_says_so(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _with_preset(workspace)
    _agent_folder(workspace)
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "ok    agent: claude-code, both agent files filled in" in out
    assert "warn" not in out


def test_an_unready_agent_setup_is_named_and_never_shown(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace)
    _agent_folder(workspace, profiles="", read=True, write=True, model=True, env_secret=True)
    assert main(["check"]) == 0  # warnings, not a refusal: the configuration itself is valid
    out = capsys.readouterr().out
    for line in (
        "warn  agent: .env has no JOTFLOW_RUNNER_SECRET",
        "warn  agent: COMPOSE_PROFILES in .env does not include agent",
        "warn  agent: JOTFLOW_GITHUB_READ_TOKEN is blank in agent.env",
        "warn  agent: JOTFLOW_GITHUB_WRITE_TOKEN is blank in agent-exec.env",
        "warn  agent: ANTHROPIC_API_KEY is blank in agent.env, which passes it to the agent",
    ):
        assert line in out, out
    for secret in (SECRET_A, SECRET_B, READ_TOKEN, WRITE_TOKEN, MODEL_KEY):
        assert secret not in out


def test_missing_files_and_preset_mismatch_are_named(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace)
    _agent_folder(workspace, exec_preset=True)
    (workspace / "agent.env").unlink()
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert f"warn  agent: agent.env is missing; run {JOTFLOW} init again to write it" in out
    assert "agent-exec.env names codex, but jotflow.toml's [agent] preset is claude-code" in out


def test_codex_teams_run_claude_code_for_execution(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace, "codex")
    _agent_folder(workspace)
    (workspace / "agent.env").write_text(
        (workspace / "agent.env").read_text().replace("claude-code", "codex")
    )
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "agent-exec.env names" not in out and "agent.env names" not in out


def test_the_command_preset_needs_a_command(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace, "command")
    _agent_folder(workspace)
    assert main(["check"]) == 0
    assert "JOTFLOW_AGENT_COMMAND is blank in agent.env" in capsys.readouterr().out


def test_without_dotenv_the_agent_files_are_not_checked_here(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace)
    assert main(["check"]) == 0
    assert (
        f"not checked here: run {JOTFLOW} check in your JotFlow folder" in capsys.readouterr().out
    )


def test_under_codex_agent_exec_must_run_claude_code(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace, "codex")
    _agent_folder(workspace, exec_preset=True)  # agent-exec.env names codex
    (workspace / "agent.env").write_text(
        (workspace / "agent.env").read_text().replace("claude-code", "codex")
    )
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "warn  agent: agent-exec.env names codex, but execution runs need claude-code" in out
    assert "agent.env names" not in out.replace("agent-exec.env names", "")


def test_an_unknown_preset_is_never_echoed(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace)
    _agent_folder(workspace)
    agent = workspace / "agent.env"
    agent.write_text(
        agent.read_text().replace(
            "JOTFLOW_AGENT_PRESET=claude-code", "JOTFLOW_AGENT_PRESET=my-secret-SENTINEL"
        )
    )
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "agent.env names an unknown preset, but jotflow.toml's [agent] preset is" in out
    assert "SENTINEL" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("name", ["agent.env", "agent-exec.env"])
def test_an_unreadable_agent_file_is_a_warning_not_a_crash(
    workspace: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    _with_preset(workspace)
    _agent_folder(workspace)
    (workspace / name).chmod(0)
    try:
        assert main(["check"]) == 0
    finally:
        (workspace / name).chmod(0o600)
    assert f"warn  agent: {name} can't be read by this user" in capsys.readouterr().out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_dotenv_is_refused_clearly(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace)
    _agent_folder(workspace)
    (workspace / ".env").chmod(0)
    try:
        assert main(["check"]) == check_command.EXIT_REFUSED
    finally:
        (workspace / ".env").chmod(0o600)
    err = capsys.readouterr().err
    assert "jotflow check: can't read .env as this user" in err
    assert SECRET_A not in err


def test_readiness_names_an_unreadable_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("x=1\n")
    policy = parse_policy('[agent]\npreset = "claude-code"\n' + ONE_CHANNEL)

    def denied(path: Path) -> dict[str, str]:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(check_command, "read_env", denied)
    assert check_command.agent_readiness(policy, tmp_path) == [".env can't be read by this user"]


# A login key the runner would never pass (#239)


def _drop_allow(path: Path, replacement: str = "") -> None:
    lines = [
        x for x in path.read_text().splitlines() if not x.startswith("JOTFLOW_AGENT_ENV_ALLOW=")
    ]
    path.write_text("\n".join([*lines, replacement]) + "\n")


def test_a_set_key_the_runner_never_passes_fails_and_names_the_line(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace)
    _agent_folder(workspace)
    _drop_allow(workspace / "agent.env")
    _drop_allow(workspace / "agent-exec.env", "JOTFLOW_AGENT_ENV_ALLOW=HTTPS_PROXY")
    assert main(["check"]) == check_command.EXIT_FAILED
    out = capsys.readouterr().out
    assert (
        "fail  agent: agent.env sets ANTHROPIC_API_KEY but doesn't pass it to the agent, so no "
        "run can sign in. Set this line in agent.env: JOTFLOW_AGENT_ENV_ALLOW=ANTHROPIC_API_KEY"
    ) in out
    assert (
        "Set this line in agent-exec.env: JOTFLOW_AGENT_ENV_ALLOW=HTTPS_PROXY,ANTHROPIC_API_KEY"
    ) in out
    assert "ok    agent" not in out and MODEL_KEY not in out


def test_a_claude_account_login_needs_no_key_line(workspace: Path) -> None:
    _with_preset(workspace)
    _agent_folder(workspace)
    for name in ("agent.env", "agent-exec.env"):
        path = workspace / name
        kept = [x for x in path.read_text().splitlines() if not x.startswith("ANTHROPIC_API_KEY=")]
        path.write_text("\n".join(kept) + "\n")
        _drop_allow(path)
    assert main(["check"]) == 0


def test_codex_fails_on_its_own_key_and_checks_execution_for_claude_code(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_preset(workspace, "codex")
    _agent_folder(workspace)
    agent = workspace / "agent.env"
    agent.write_text(
        agent.read_text().replace("JOTFLOW_AGENT_PRESET=claude-code", "JOTFLOW_AGENT_PRESET=codex")
        + "OPENAI_API_KEY=sk-proj-SENTINEL\n"
    )
    # agent.env passes only ANTHROPIC_API_KEY, which codex doesn't read.
    assert main(["check"]) == check_command.EXIT_FAILED
    out = capsys.readouterr().out
    assert "agent.env sets OPENAI_API_KEY but doesn't pass it" in out
    assert "JOTFLOW_AGENT_ENV_ALLOW=ANTHROPIC_API_KEY,OPENAI_API_KEY" in out
    assert "agent-exec.env sets" not in out and "SENTINEL" not in out


def test_the_command_preset_has_no_login_to_check(workspace: Path) -> None:
    from jotflow.policy import load_policy

    _with_preset(workspace, "command")
    _agent_folder(workspace)
    _drop_allow(workspace / "agent.env")
    assert check_command.agent_failures(load_policy(workspace / "jotflow.toml"), workspace) == []
