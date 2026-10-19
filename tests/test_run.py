import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from slack_sdk.errors import SlackApiError

from jotflow.commands import run as run_command
from jotflow.orchestrator import service as service_module
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
        self.commands: dict[str, Callable[..., Any]] = {}
        self.actions: dict[str, Callable[..., Any]] = {}
        self.views: dict[str, Callable[..., Any]] = {}

    def action(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.actions[name] = fn
            return fn

        return register

    def view(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.views[name] = fn
            return fn

        return register

    def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.commands[name] = fn
            return fn

        return register

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
    assert set(app.listeners) == {"message", "reaction_added", "reaction_removed"}
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


class UnreachableClient(FakeClient):
    async def auth_test(self) -> dict[str, Any]:
        raise aiohttp.ClientConnectionError("no route")

    async def conversations_info(self, channel: str) -> dict[str, Any]:
        raise TimeoutError


async def test_unreachable_slack_is_a_setup_error() -> None:
    with pytest.raises(SlackSetupError, match="could not reach Slack"):
        await fetch_identity(UnreachableClient())  # type: ignore[arg-type]
    with pytest.raises(SlackSetupError, match="could not reach Slack"):
        await missing_channels(UnreachableClient(), ["C0IN"])  # type: ignore[arg-type]


# serve(): the Bolt app and Socket Mode handler are replaced with fakes, so
# these tests drive the whole startup, wait, and shutdown path with no network.


class FakeBoltApp(FakeApp):
    def __init__(self, client: FakeClient) -> None:
        super().__init__()
        self.client = client


class FakeHandler(FakeConnection):
    def __init__(self, error: BaseException | None = None) -> None:
        super().__init__()
        self.error = error

    async def connect_async(self) -> None:
        await super().connect_async()
        if self.error is not None:
            raise self.error


class FakeMcp:
    """Stands in for the MCP server so serve() tests never bind a port."""

    def __init__(self, calls: list[str], start_error: BaseException | None) -> None:
        self.calls = calls
        self.start_error = start_error

    async def start(self) -> None:
        self.calls.append("mcp start")
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.calls.append("mcp stop")


class ServeHarness:
    """Patches serve()'s collaborators and records what it did."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
        self.app = FakeBoltApp(client)
        self.handlers: list[FakeHandler] = []
        self.handler_error: BaseException | None = None
        self.signals_removed = False
        self.mcp_calls: list[str] = []
        self.mcp_start_error: BaseException | None = None
        monkeypatch.setattr(run_command, "AsyncApp", lambda **_: self.app)
        # The packet reader copies the real client's settings; the fake has none.
        monkeypatch.setattr(service_module, "BoltSlackReader", lambda client: client)
        monkeypatch.setattr(
            run_command, "make_mcp_server", lambda *_: FakeMcp(self.mcp_calls, self.mcp_start_error)
        )
        monkeypatch.setattr(run_command, "AsyncSocketModeHandler", self._handler)
        monkeypatch.setattr(run_command, "install_stop_signals", self._install)

    def _handler(self, app: Any, app_token: str) -> FakeHandler:
        assert app is self.app
        assert app_token == "xapp-test"
        handler = FakeHandler(self.handler_error)
        self.handlers.append(handler)
        return handler

    def _install(self, stop: asyncio.Event) -> Callable[[], None]:
        # Stand in for a SIGTERM arriving once the connection is up.
        asyncio.get_running_loop().call_soon(stop.set)

        def remove() -> None:
            self.signals_removed = True

        return remove


@pytest.fixture(autouse=True)
def _private_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # serve() opens the database at its default relative path.
    monkeypatch.chdir(tmp_path)


def serve_inputs() -> tuple[Any, Any]:
    from jotflow.config import Settings
    from jotflow.policy import parse_policy

    settings = Settings(slack_bot_token="xoxb-test", slack_app_token="xapp-test")
    return settings, parse_policy(POLICY)


def member_client() -> FakeClient:
    return FakeClient(channels={"C0TEST1": {"is_member": True}})


async def test_serve_connects_waits_and_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="jotflow")
    harness = ServeHarness(monkeypatch, member_client())
    assert await run_command.serve(*serve_inputs()) == 0
    [handler] = harness.handlers
    assert handler.calls == ["connect", "close"]
    assert harness.signals_removed
    assert harness.mcp_calls == ["mcp start", "mcp stop"]
    assert set(harness.app.listeners) == {"message", "reaction_added", "reaction_removed"}
    assert "watching 1 configured channel(s) as @jotbot" in caplog.text
    assert "not a member" not in caplog.text


async def test_serve_reports_channels_to_invite(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="jotflow")
    ServeHarness(monkeypatch, FakeClient())
    assert await run_command.serve(*serve_inputs()) == 0
    assert "not a member of channel C0TEST1; invite the bot there with /invite @jotbot" in (
        caplog.text
    )


async def test_serve_stops_on_a_setup_error_before_connecting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = ServeHarness(monkeypatch, FakeClient(auth="invalid_auth"))
    assert await run_command.serve(*serve_inputs()) == run_command.EXIT_SLACK
    assert harness.handlers == []
    assert harness.mcp_calls == []
    assert "invalid_auth" in caplog.text


async def test_serve_reports_a_refused_socket_mode_connection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = ServeHarness(monkeypatch, member_client())
    harness.handler_error = slack_error("invalid_auth")
    assert await run_command.serve(*serve_inputs()) == run_command.EXIT_SLACK
    [handler] = harness.handlers
    assert handler.calls == ["connect", "close"]
    assert harness.signals_removed
    assert "Slack refused the Socket Mode connection (invalid_auth)" in caplog.text
    assert harness.mcp_calls == ["mcp start", "mcp stop"]


async def test_serve_stops_when_the_mcp_server_cannot_start(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from jotflow.whiteboard.server import McpStartError

    harness = ServeHarness(monkeypatch, member_client())
    harness.mcp_start_error = McpStartError("could not listen on 127.0.0.1:8765")
    assert await run_command.serve(*serve_inputs()) == run_command.EXIT_MCP
    assert harness.handlers == []
    assert "could not listen on 127.0.0.1:8765" in caplog.text


def test_make_mcp_server_uses_the_settings() -> None:
    from jotflow.whiteboard.tokens import TokenRegistry

    settings, _ = serve_inputs()
    server = run_command.make_mcp_server(settings, TokenRegistry())
    assert (server.host, server.port) == ("127.0.0.1", 8765)


async def test_serve_reports_an_unreachable_slack_on_connect(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = ServeHarness(monkeypatch, member_client())
    harness.handler_error = aiohttp.ClientConnectionError("no route")
    assert await run_command.serve(*serve_inputs()) == run_command.EXIT_SLACK
    [handler] = harness.handlers
    assert handler.calls == ["connect", "close"]
    assert harness.signals_removed
    assert "could not reach Slack (ClientConnectionError)" in caplog.text


def test_ctrl_c_before_signals_are_installed_exits_130(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    (clean_env / "jotflow.toml").write_text(POLICY, encoding="utf-8")

    async def interrupted(settings: Any, policy: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_command, "serve", interrupted)
    assert run_command.run(args()) == 130


async def test_serve_stops_when_the_database_cannot_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    harness = ServeHarness(monkeypatch, member_client())
    settings, policy = serve_inputs()
    settings = settings.model_copy(update={"database_path": str(tmp_path / "missing" / "j.db")})
    assert await run_command.serve(settings, policy) == run_command.EXIT_STORE
    assert "could not open the database" in caplog.text
    assert "JOTFLOW_DATABASE_PATH" in caplog.text
    assert harness.handlers == [] and harness.mcp_calls == []


async def test_serve_opens_the_store_and_closes_it_after_slack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = ServeHarness(monkeypatch, member_client())
    assert await run_command.serve(*serve_inputs()) == 0
    assert (tmp_path / "jotflow.db").exists()
    [handler] = harness.handlers
    assert handler.calls == ["connect", "close"]
    assert harness.mcp_calls == ["mcp start", "mcp stop"]


async def test_serve_registers_the_card_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = ServeHarness(monkeypatch, member_client())
    assert await run_command.serve(*serve_inputs()) == 0
    assert set(harness.app.actions) == {
        "jotflow_approve",
        "jotflow_retry",
        "jotflow_discard",
        "jotflow_request_changes",
        # The card's edit controls (#160).
        "jotflow_repo",
        "jotflow_assign",
        "jotflow_unassign",
    }
    assert set(harness.app.views) == {"jotflow_changes"}


async def test_serve_stops_the_orchestrator_if_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = ServeHarness(monkeypatch, member_client())

    def broken(*_: Any) -> None:
        raise RuntimeError("bolt refused")

    monkeypatch.setattr(run_command, "register_actions", broken)
    with pytest.raises(RuntimeError, match="bolt refused"):
        await run_command.serve(*serve_inputs())
    assert harness.mcp_calls == ["mcp start", "mcp stop"]
