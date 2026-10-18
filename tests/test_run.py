import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from jotflow.commands import run as run_command
from jotflow.orchestrator.ingest import BotIdentity, EventFilter
from jotflow.orchestrator.slack import (
    SlackSetupError,
    fetch_identity,
    missing_channels,
    register_handlers,
    report_missing,
)

SECRET_TEXT = "the launch code is swordfish"

POLICY = """
[channels.C0TEST1]

[[channels.C0TEST1.repos]]
repo = "acme/web"
nickname = "web"
description = "The web app"
"""


def slack_error(error: str) -> SlackApiError:
    return SlackApiError(error, {"ok": False, "error": error})


class FakeClient:
    def __init__(
        self,
        channels: dict[str, dict[str, Any] | str] | None = None,
        auth: dict[str, Any] | str | None = None,
    ) -> None:
        self.channels = channels or {}
        self.auth = (
            auth if auth is not None else {"user_id": "U0BOT", "bot_id": "B0BOT", "user": "jotbot"}
        )

    async def auth_test(self) -> dict[str, Any]:
        if isinstance(self.auth, str):
            raise slack_error(self.auth)
        return self.auth

    async def conversations_info(self, channel: str) -> dict[str, Any]:
        result = self.channels.get(channel, "channel_not_found")
        if isinstance(result, str):
            raise slack_error(result)
        return {"ok": True, "channel": result}


class FakeApp:
    def __init__(self) -> None:
        self.listeners: dict[str, Callable[..., Any]] = {}

    def event(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.listeners[name] = fn
            return fn

        return register


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def connect_async(self) -> None:
        self.calls.append("connect")

    async def close_async(self) -> None:
        self.calls.append("close")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("JOTFLOW_"):
            monkeypatch.delenv(name)
    return tmp_path


@pytest.fixture(autouse=True)
def restore_logging() -> Any:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    libs = {name: logging.getLogger(name).level for name in run_command.THIRD_PARTY_LOGGERS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lib_level in libs.items():
        logging.getLogger(name).setLevel(lib_level)


def args(level: str = "info") -> argparse.Namespace:
    return argparse.Namespace(log_level=level)


def test_missing_tokens_exit_2_before_any_network(
    clean_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_command, "serve", pytest.fail)
    assert run_command.run(args()) == 2
    err = capsys.readouterr().err
    assert "JOTFLOW_SLACK_BOT_TOKEN: not set" in err
    assert "JOTFLOW_SLACK_APP_TOKEN: not set" in err


def test_bad_policy_exits_2_before_any_network(
    clean_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    (clean_env / "jotflow.toml").write_text("issue_link = 'none'\n" + POLICY, encoding="utf-8")
    monkeypatch.setattr(run_command, "serve", pytest.fail)
    assert run_command.run(args()) == 2
    assert "issue_link: unknown key" in capsys.readouterr().err


def test_missing_policy_file_exits_2(
    clean_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    assert run_command.run(args()) == 2
    assert "file not found" in capsys.readouterr().err


def test_valid_config_reaches_serve(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    (clean_env / "jotflow.toml").write_text(POLICY, encoding="utf-8")
    seen: list[str] = []

    async def fake_serve(settings: Any, policy: Any) -> int:
        seen.extend(policy.channels)
        return 0

    monkeypatch.setattr(run_command, "serve", fake_serve)
    assert run_command.run(args()) == 0
    assert seen == ["C0TEST1"]


def test_debug_logging_keeps_slack_libraries_at_info() -> None:
    run_command.configure_logging("debug")
    assert logging.getLogger("jotflow").getEffectiveLevel() == logging.DEBUG
    for name in run_command.THIRD_PARTY_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO


def test_warning_level_applies_to_slack_libraries_too() -> None:
    run_command.configure_logging("warning")
    assert logging.getLogger("slack_sdk").level == logging.WARNING


async def test_run_until_stopped_connects_then_closes() -> None:
    connection = FakeConnection()
    stop = asyncio.Event()
    task = asyncio.create_task(run_command.run_until_stopped(connection, stop))
    await asyncio.sleep(0)
    assert connection.calls == ["connect"]
    stop.set()
    await task
    assert connection.calls == ["connect", "close"]


async def test_cancellation_still_closes() -> None:
    connection = FakeConnection()
    task = asyncio.create_task(run_command.run_until_stopped(connection, asyncio.Event()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.calls == ["connect", "close"]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signals_set_the_stop_event(sig: signal.Signals) -> None:
    stop = asyncio.Event()
    remove = run_command.install_stop_signals(stop)
    try:
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(stop.wait(), timeout=2)
    finally:
        remove()
    assert stop.is_set()


async def test_fetch_identity() -> None:
    identity, name = await fetch_identity(FakeClient())  # type: ignore[arg-type]
    assert identity == BotIdentity(user_id="U0BOT", bot_id="B0BOT")
    assert name == "jotbot"


async def test_fetch_identity_reports_a_bad_token() -> None:
    with pytest.raises(SlackSetupError, match="invalid_auth"):
        await fetch_identity(FakeClient(auth="invalid_auth"))  # type: ignore[arg-type]


async def test_missing_channels_reports_non_members_and_invisible_ones() -> None:
    client = FakeClient(channels={"C0IN": {"is_member": True}, "C0OUT": {"is_member": False}})
    missing = await missing_channels(client, ["C0IN", "C0OUT", "G0PRIVATE"])  # type: ignore[arg-type]
    assert missing == ["C0OUT", "G0PRIVATE"]


async def test_missing_scope_is_a_setup_error() -> None:
    client = FakeClient(channels={"C0IN": "missing_scope"})
    with pytest.raises(SlackSetupError, match="channels:read"):
        await missing_channels(client, ["C0IN"])  # type: ignore[arg-type]


def test_missing_channels_are_reported_with_invite_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="jotflow")
    report_missing(["C0OUT"], "jotbot")
    assert "not a member of channel C0OUT; invite the bot there with /invite @jotbot" in (
        caplog.text
    )


async def test_registered_listener_logs_pointer_not_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="jotflow")
    app = FakeApp()
    register_handlers(app, EventFilter(["C0TEST1"], BotIdentity("U0BOT")))  # type: ignore[arg-type]
    assert set(app.listeners) == {"message", "reaction_added"}
    event = {
        "type": "message",
        "channel": "C0TEST1",
        "user": "U0A",
        "text": SECRET_TEXT,
        "ts": "1.1",
    }
    await app.listeners["message"](event=event, body={"event_id": "Ev1", "event": event})
    await app.listeners["message"](event=event, body={"event_id": "Ev1", "event": event})
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info) == 1
    assert "channel=C0TEST1 ts=1.1" in info[0].getMessage()
    assert SECRET_TEXT not in caplog.text
