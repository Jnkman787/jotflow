"""`jotflow operator`: read-only tokens for a person's terminal agent (#58, D12).

An admin runs these inside the JotFlow container::

    docker compose exec jotflow jotflow operator create --name alex --channel C0123ABCD
    docker compose exec jotflow jotflow operator list
    docker compose exec jotflow jotflow operator revoke alex

``create`` prints the token once; only its SHA-256 digest is stored. A token
lasts 30 days unless ``--days`` says otherwise, and at most 365. A running
``jotflow run`` accepts a new token at once and drops a revoked one within
2 seconds.

``connect`` is the stdio bridge a terminal MCP client starts as its server
command. No port is published: the client runs, on the Docker host or over
ssh to it::

    docker compose exec -T -e JOTFLOW_OPERATOR_TOKEN jotflow jotflow operator connect

``-e NAME`` with no value copies the variable from the caller's environment,
so the token never appears in any command line. The bridge speaks MCP on its
stdin and stdout and forwards ``tools/list`` and ``tools/call`` to the MCP
endpoint inside the container, on loopback, with the token as its bearer
token. It serves no other capability, and it has no tools of its own: the
server decides what the token may call, which is ``list_topics`` and
``get_topic`` on the token's channels.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sqlite3
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
)

from jotflow import __version__
from jotflow.commands.run import SettingsError, load_settings
from jotflow.policy import Policy, PolicyError, UnknownChannelError, load_policy
from jotflow.store import ConflictError, OperatorToken, SqliteStore, Store, StoreError
from jotflow.store.base import utc_now
from jotflow.whiteboard.operator_tokens import OPERATOR_PREFIX, digest
from jotflow.whiteboard.server import PATH, PINNED_VERSIONS
from jotflow.whiteboard.tokens import MAX_TOKEN_LENGTH

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_STORE = 4
EXIT_BRIDGE = 5

DEFAULT_DAYS = 30
MAX_DAYS = 365
TOKEN_ENV = "JOTFLOW_OPERATOR_TOKEN"
PORT_ENV = "JOTFLOW_MCP_PORT"
DEFAULT_PORT = 8765
BRIDGE_TIMEOUT_SECONDS = 30.0

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class Refusal(Exception):
    """The command will not run. The message says why."""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", metavar="<action>")
    create = sub.add_parser("create", help="Issue a token and print it once")
    create.add_argument("--name", required=True, help="Who it is for: lowercase, unique")
    create.add_argument(
        "--channel",
        dest="channels",
        action="append",
        required=True,
        metavar="CHANNEL_ID",
        help="A channel from jotflow.toml the token may read; repeat for more",
    )
    create.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"How long it lasts (default {DEFAULT_DAYS}, at most {MAX_DAYS})",
    )
    sub.add_parser("list", help="Every token's name, channels, expiry and last use; no secrets")
    revoke = sub.add_parser("revoke", help="Stop a token working, within 2 seconds")
    revoke.add_argument("name")
    sub.add_parser(
        "connect",
        help=f"The stdio bridge for a terminal MCP client; reads the token from {TOKEN_ENV}",
    )
    parser.set_defaults(operator_usage=parser.format_usage())


# create, list, revoke


def new_token(
    name: str, channels: Sequence[str], days: int, policy: Policy, now: datetime
) -> tuple[str, OperatorToken]:
    """A fresh token and the record that stores its digest. Checks every argument."""
    if not _NAME.fullmatch(name):
        raise Refusal(
            "the name must be 1 to 64 lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    if not 1 <= days <= MAX_DAYS:
        raise Refusal(f"--days must be between 1 and {MAX_DAYS}")
    unique = list(dict.fromkeys(channels))
    for channel in unique:
        try:
            policy.channel(channel)
        except UnknownChannelError:
            raise Refusal(f"channel {channel} is not in jotflow.toml") from None
    token = OPERATOR_PREFIX + secrets.token_urlsafe(32)
    record = OperatorToken(
        id=secrets.token_hex(8),
        name=name,
        digest=digest(token),
        channels=tuple(unique),
        created_at=now,
        expires_at=now + timedelta(days=days),
    )
    return token, record


def status(token: OperatorToken, now: datetime) -> str:
    if token.revoked_at is not None:
        return "revoked"
    return "active" if token.usable(now) else "expired"


def _when(value: datetime | None) -> str:
    return "never" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def listing(tokens: Sequence[OperatorToken], now: datetime) -> list[str]:
    """One line per token, newest first. Never a token or a digest."""
    if not tokens:
        return ["no operator tokens"]
    rows = [("NAME", "ID", "STATUS", "CHANNELS", "EXPIRES", "LAST USED")]
    rows += [
        (
            t.name,
            t.id,
            status(t, now),
            ",".join(t.channels),
            _when(t.expires_at),
            _when(t.last_used_at),
        )
        for t in tokens
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


@asynccontextmanager
async def _opened(path: str) -> AsyncIterator[Store]:
    store = await SqliteStore.open(path)
    try:
        yield store
    finally:
        await store.close()


async def create(store: Store, record: OperatorToken) -> None:
    try:
        await store.add_operator_token(record)
    except ConflictError:
        raise Refusal(
            f"a token named {record.name} already exists; names are never reused, "
            "so the audit log stays unambiguous"
        ) from None


async def revoke(store: Store, name: str) -> OperatorToken:
    revoked = await store.revoke_operator_token(name)
    if revoked is None:
        raise Refusal(f"no live operator token named {name}")
    return revoked


def _admin(args: argparse.Namespace) -> int:
    settings = load_settings()
    now = utc_now()
    if args.action == "create":
        policy = load_policy(settings.config_path)
        token, record = new_token(args.name, args.channels, args.days, policy, now)

        async def _create() -> None:
            async with _opened(settings.database_path) as store:
                await create(store, record)

        asyncio.run(_create())
        print(
            f"operator token {record.name} ({record.id}) reads {', '.join(record.channels)} "
            f"until {_when(record.expires_at)}. It is shown once; store it now:",
            file=sys.stderr,
        )
        print(token)
        return EXIT_OK
    if args.action == "list":

        async def _list() -> list[OperatorToken]:
            async with _opened(settings.database_path) as store:
                return await store.operator_tokens()

        print("\n".join(listing(asyncio.run(_list()), now)))
        return EXIT_OK

    async def _revoke() -> OperatorToken:
        async with _opened(settings.database_path) as store:
            return await revoke(store, args.name)

    revoked = asyncio.run(_revoke())
    print(f"operator token {revoked.name} ({revoked.id}) is revoked; it stops within 2 seconds")
    return EXIT_OK


# connect: the stdio bridge


def bridge_token(environ: dict[str, str] | os._Environ[str]) -> str:
    token = environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise Refusal(f"set {TOKEN_ENV} to the operator token; it is never read from arguments")
    if (
        not token.startswith(OPERATOR_PREFIX)
        or len(token) > MAX_TOKEN_LENGTH
        or any(c.isspace() for c in token)
    ):
        raise Refusal(f"{TOKEN_ENV} does not hold an operator token")
    return token


def bridge_url(environ: dict[str, str] | os._Environ[str]) -> str:
    """The MCP endpoint on loopback inside the container. Never another host."""
    raw = environ.get(PORT_ENV, "").strip() or str(DEFAULT_PORT)
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise Refusal(f"{PORT_ENV} must be a port number")
    return f"http://127.0.0.1:{int(raw)}{PATH}"


def build_bridge(upstream: Client) -> Server[Any]:
    """A stdio MCP server whose only handlers forward to ``upstream``.

    The tool list and every call result are the server's own; the bridge adds
    nothing and decides nothing.
    """

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return await upstream.list_tools(cursor=None if params is None else params.cursor)

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult:
        return await upstream.call_tool(params.name, params.arguments)

    return Server(
        "jotflow-operator",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


@asynccontextmanager
async def upstream_client(url: str, token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=BRIDGE_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode=PINNED_VERSIONS[0]) as client:
            yield client


async def serve_bridge(url: str, token: str) -> None:
    async with upstream_client(url, token) as upstream:
        # Fail fast, on stderr, if the token is refused or the server is down.
        await upstream.list_tools()
        bridge = build_bridge(upstream)
        async with stdio_server() as (read, write):
            await bridge.run(read, write, bridge.create_initialization_options())


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in _leaves(sub)]
    return [error]


def _connect() -> int:
    token = bridge_token(os.environ)
    url = bridge_url(os.environ)
    try:
        try:
            anyio.run(serve_bridge, url, token)
        except BaseExceptionGroup as group:
            leaves = _leaves(group)
            raise leaves[0] if len(leaves) == 1 else group from None
    except MCPError as exc:
        # The server's own words, such as "the bearer token is not valid".
        print(f"jotflow operator connect: refused: {exc}", file=sys.stderr)
        return EXIT_BRIDGE
    except (OSError, httpx2.HTTPError) as exc:
        print(
            f"jotflow operator connect: could not reach JotFlow's MCP endpoint "
            f"({type(exc).__name__}); is jotflow run up?",
            file=sys.stderr,
        )
        return EXIT_BRIDGE
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if getattr(args, "action", None) is None:
        print(
            getattr(args, "operator_usage", "") + "choose create, list, revoke or connect",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        if args.action == "connect":
            return _connect()
        return _admin(args)
    except Refusal as exc:
        print(f"jotflow operator {args.action}: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (SettingsError, PolicyError) as exc:
        print(f"jotflow operator {args.action}: configuration problem in {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (StoreError, OSError, sqlite3.Error) as exc:
        print(
            f"jotflow operator {args.action}: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_STORE
