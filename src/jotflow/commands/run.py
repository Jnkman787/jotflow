"""`jotflow run`: connect to Slack over Socket Mode and run the whiteboard.

Loads settings and policy first, so a missing token or a bad ``jotflow.toml``
stops the command before any network call. Then it checks channel
membership, starts the :class:`~jotflow.orchestrator.service.Orchestrator`
(store, MCP server, debouncer, and agent runs), connects, and handles events
until SIGINT or SIGTERM. Slack closes first on the way out, then the
orchestrator in reverse order.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import Callable, Sequence
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
from jotflow.orchestrator.approval import register_actions
from jotflow.orchestrator.card_edits import register_edits
from jotflow.orchestrator.health import ConnectionWatch, Heartbeat, Watched, heartbeat_path
from jotflow.orchestrator.ingest import EventFilter
from jotflow.orchestrator.service import Orchestrator
from jotflow.orchestrator.slack import (
    NETWORK_ERRORS,
    SlackSetupError,
    fetch_identity,
    missing_channels,
    register_commands,
    register_handlers,
    report_missing,
)
from jotflow.orchestrator.summary import register_summary
from jotflow.policy import Policy, PolicyError, load_policy
from jotflow.store import StoreError
from jotflow.store.lock import StoreBusyError
from jotflow.whiteboard.server import McpServer, McpStartError, build_app
from jotflow.whiteboard.tokens import TokenCheck
from jotflow.whiteboard.tools import ToolSpec

log = logging.getLogger("jotflow.run")

LOG_LEVELS = ("debug", "info", "warning", "error")

EXIT_CONFIG = 2
EXIT_SLACK = 1
EXIT_MCP = 3
EXIT_STORE = 4


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


def _watched(connection: object) -> Watched | None:
    """What reports whether Socket Mode is connected: the handler's client, or a fake."""
    for candidate in (connection, getattr(connection, "client", None)):
        if candidate is not None and callable(getattr(candidate, "is_connected", None)):
            return candidate  # type: ignore[return-value]
    return None


async def run_until_stopped(connection: Connection, stop: asyncio.Event) -> None:
    """Connect, wait for ``stop``, then close, even if waiting is cancelled.

    While connected, a :class:`ConnectionWatch` logs drops and reconnects and
    keeps the heartbeat ``jotflow health`` reads (#133).
    """
    await connection.connect_async()
    log.info("connected to Slack over Socket Mode")
    watched = _watched(connection)
    heartbeat = Heartbeat(heartbeat_path())
    watching = asyncio.Event()
    watch = (
        asyncio.create_task(ConnectionWatch(watched, heartbeat).run(watching), name="jotflow-watch")
        if watched is not None
        else None
    )
    if watch is None:
        log.warning(
            "the Slack connection does not report whether it is connected, so there is no "
            "heartbeat and jotflow health will say unhealthy"
        )
    try:
        await stop.wait()
        log.info("shutting down")
    finally:
        watching.set()
        if watch is not None:
            with contextlib.suppress(Exception):
                await watch
        # Stopping on purpose is not a health signal to keep.
        with contextlib.suppress(OSError):
            heartbeat.path.unlink()
        await connection.close_async()
        log.info("disconnected")


def make_mcp_server(
    settings: Settings, tokens: TokenCheck, tools: Sequence[ToolSpec] = ()
) -> McpServer:
    """The whiteboard MCP server, serving ``tools`` to tokens ``tokens`` issued.

    The run lifecycle issues a token per run and revokes it when the run ends,
    so with agent runs off nothing is issued and nothing is reachable.
    """
    app = build_app(
        port=settings.mcp_port,
        extra_hosts=settings.mcp_allowed_hosts,
        tokens=tokens,
        tools=tools,
    )
    return McpServer(app, settings.mcp_host, settings.mcp_port)


async def serve(settings: Settings, policy: Policy) -> int:
    app = AsyncApp(
        token=settings.slack_bot_token.get_secret_value(),
        logger=logging.getLogger("slack_bolt"),
    )
    try:
        identity, bot_name = await fetch_identity(app.client)
        missing = await missing_channels(app.client, policy.channels)
        report_missing(missing, bot_name)
    except SlackSetupError as exc:
        log.error("%s", exc)
        return EXIT_SLACK

    orchestrator = Orchestrator(settings, policy, app.client, identity, make_server=make_mcp_server)
    try:
        await orchestrator.start()
    except McpStartError as exc:
        log.error("%s", exc)
        return EXIT_MCP
    except StoreBusyError as exc:
        log.error("%s", exc)
        return EXIT_STORE
    except (StoreError, OSError, sqlite3.Error) as exc:
        # The type and path only: an error's text could, in principle, quote a row.
        log.error(
            "could not open the database at %s (%s); check JOTFLOW_DATABASE_PATH",
            settings.database_path,
            type(exc).__name__,
        )
        return EXIT_STORE
    try:
        register_handlers(app, EventFilter(policy.channels, identity), orchestrator.note)
        register_commands(
            app,
            policy,
            orchestrator.channel_mode,
            orchestrator.modes,
            orchestrator.links,
            orchestrator.verify,
            orchestrator.repos,
        )
        if orchestrator.approval is not None:
            register_actions(app, orchestrator.approval)
        if orchestrator.edits is not None:
            register_edits(app, orchestrator.edits)
        if orchestrator.summaries is not None and orchestrator.approval is not None:
            register_summary(
                app,
                orchestrator.summaries,
                orchestrator.approval,
                orchestrator.summary_slack,
                orchestrator.summary_names,
            )
        await orchestrator.ensure_guides([c for c in policy.channels if c not in missing])
    except BaseException:
        # Started is stopped, whatever fails after it.
        await orchestrator.stop()
        raise
    log.info("watching %d configured channel(s) as @%s", len(policy.channels), bot_name)

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
        # Slack is closed by now; then runs, tokens, the MCP server, and the store.
        await orchestrator.stop()
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
