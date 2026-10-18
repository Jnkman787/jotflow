"""`jotflow run`: connect to Slack over Socket Mode and log incoming events.

Loads settings and policy first, so a missing token or a bad ``jotflow.toml``
stops the command before any network call. Then it checks channel
membership, connects, and logs each event as pointers only until SIGINT or
SIGTERM.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from jotflow.config import Settings
from jotflow.logs import (
    THIRD_PARTY_LOGGERS as THIRD_PARTY_LOGGERS,  # re-exported: callers read it here
)
from jotflow.logs import (
    BodiesRefused,
    check_bodies_allowed,
    configure_logging,
)
from jotflow.orchestrator.ingest import EventFilter
from jotflow.orchestrator.slack import (
    NETWORK_ERRORS,
    SlackSetupError,
    fetch_identity,
    missing_channels,
    register_handlers,
    report_missing,
)
from jotflow.policy import Policy, PolicyError, load_policy
from jotflow.whiteboard.server import McpServer, McpStartError, build_app
from jotflow.whiteboard.tokens import TokenRegistry

log = logging.getLogger("jotflow.run")

LOG_LEVELS = ("debug", "info", "warning", "error")

EXIT_CONFIG = 2
EXIT_SLACK = 1
EXIT_MCP = 3


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register this command's options."""
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="info",
        help="JotFlow's log level (default: info). Message text and tokens are never logged.",
    )
    parser.add_argument(
        "--log-bodies",
        action="store_true",
        help="Local development only: show Slack message text in library logs. Tokens stay "
        "redacted. Refused in CI and inside JotFlow's image.",
    )


def _env_name(field: str) -> str:
    return field if field.startswith("JOTFLOW_") else f"JOTFLOW_{field.upper()}"


class SettingsError(Exception):
    """Required environment variables are missing or invalid."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("environment or .env:\n" + "\n".join(f"  {p}" for p in problems))


def load_settings() -> Settings:
    """Settings from the environment, with missing variables named."""
    try:
        return Settings()  # type: ignore[call-arg]  # pydantic-settings fills these from the env
    except ValidationError as exc:
        problems = [
            f"{_env_name(str(error['loc'][0]))}: "
            + ("not set" if error["type"] == "missing" else str(error["msg"]))
            for error in exc.errors(include_url=False)
        ]
        raise SettingsError(problems) from None


class Connection(Protocol):
    async def connect_async(self) -> None: ...
    async def close_async(self) -> None: ...


def install_stop_signals(stop: asyncio.Event) -> Callable[[], None]:
    """Set ``stop`` on SIGINT or SIGTERM. Returns a function that undoes it."""
    loop = asyncio.get_running_loop()
    signals = [signal.SIGINT, signal.SIGTERM]
    try:
        for sig in signals:
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:  # pragma: no cover - Windows event loops
        previous = {
            sig: signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            for sig in signals
        }

        def restore() -> None:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

        return restore

    def remove() -> None:
        for sig in signals:
            loop.remove_signal_handler(sig)

    return remove


async def run_until_stopped(connection: Connection, stop: asyncio.Event) -> None:
    """Connect, wait for ``stop``, then close, even if waiting is cancelled."""
    await connection.connect_async()
    log.info("connected to Slack over Socket Mode")
    try:
        await stop.wait()
        log.info("shutting down")
    finally:
        await connection.close_async()
        log.info("disconnected")


def make_mcp_server(settings: Settings, tokens: TokenRegistry) -> McpServer:
    """The whiteboard MCP server, admitting only tokens ``tokens`` issued.

    The run lifecycle (#53) issues a token per run and revokes it when the run
    ends. Until runs exist nothing is issued, so nothing is reachable.
    """
    app = build_app(port=settings.mcp_port, extra_hosts=settings.mcp_allowed_hosts, tokens=tokens)
    return McpServer(app, settings.mcp_host, settings.mcp_port)


async def serve(settings: Settings, policy: Policy) -> int:
    app = AsyncApp(
        token=settings.slack_bot_token.get_secret_value(),
        logger=logging.getLogger("slack_bolt"),
    )
    try:
        identity, bot_name = await fetch_identity(app.client)
        report_missing(await missing_channels(app.client, policy.channels), bot_name)
    except SlackSetupError as exc:
        log.error("%s", exc)
        return EXIT_SLACK
    register_handlers(app, EventFilter(policy.channels, identity))
    log.info("watching %d configured channel(s) as @%s", len(policy.channels), bot_name)

    tokens = TokenRegistry()
    mcp = make_mcp_server(settings, tokens)
    try:
        await mcp.start()
    except McpStartError as exc:
        log.error("%s", exc)
        return EXIT_MCP

    handler: Connection = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
    stop = asyncio.Event()
    remove_signals = install_stop_signals(stop)
    try:
        await run_until_stopped(handler, stop)
    except SlackApiError as exc:
        log.error(
            "Slack refused the Socket Mode connection (%s); check JOTFLOW_SLACK_APP_TOKEN",
            exc.response.get("error", "unknown error"),
        )
        await handler.close_async()
        return EXIT_SLACK
    except NETWORK_ERRORS as exc:
        log.error("could not reach Slack (%s); check the network", type(exc).__name__)
        await handler.close_async()
        return EXIT_SLACK
    finally:
        remove_signals()
        await mcp.stop()
    return 0


def run(args: argparse.Namespace) -> int:
    bodies = bool(getattr(args, "log_bodies", False))
    if bodies:
        try:
            check_bodies_allowed(os.environ)
        except BodiesRefused as exc:
            print(f"jotflow run: refusing: {exc}", file=sys.stderr)
            return EXIT_CONFIG
    configure_logging(getattr(args, "log_level", "info"), bodies=bodies)
    try:
        settings = load_settings()
        policy = load_policy(settings.config_path)
    except (SettingsError, PolicyError) as exc:
        print(f"jotflow run: configuration problem in {exc}", file=sys.stderr)
        return EXIT_CONFIG
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(serve(settings, policy))
    return 130
