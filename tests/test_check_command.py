import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from jotflow.cli import main
from jotflow.commands import check as check_command
from jotflow.policy import parse_policy

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
    assert "JOTFLOW_LIVE_CHECK=1 jotflow check --live" in capsys.readouterr().err
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
