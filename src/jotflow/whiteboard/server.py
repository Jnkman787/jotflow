"""Build and run the whiteboard MCP server.

:func:`build_app` assembles the ASGI app: the :class:`~jotflow.whiteboard.gate.Gate`
in front of the MCP SDK's Streamable HTTP endpoint. :class:`McpServer` runs it
with uvicorn inside ``jotflow run``'s event loop.

JotFlow builds on the SDK's low-level ``Server``, not ``MCPServer``.
``MCPServer`` always registers prompt, resource, and subscription handlers, and
the SDK advertises a capability for every registered handler. Registering only
``tools/list`` and ``tools/call`` makes the server declare exactly
``{"tools": {"listChanged": false}}``: no prompts, resources, subscriptions,
logging, completions, sampling, or elicitation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError
from starlette.types import ASGIApp

from jotflow import __version__
from jotflow.models import RunKind
from jotflow.whiteboard.gate import GRANT_KEY, Gate
from jotflow.whiteboard.tokens import Grant, RejectAllTokens, TokenCheck
from jotflow.whiteboard.tools import TOOLS, ToolError, ToolSpec, checked_registry

log = logging.getLogger("jotflow.mcp")

PATH = "/mcp"
# The protocol versions served. Only the stateless 2026-07-28 revision: the
# handshake era brings sessions back, which #16 would have to bind to tokens.
PINNED_VERSIONS: tuple[str, ...] = ("2026-07-28",)
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0
STARTUP_TIMEOUT_SECONDS = 10.0
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

if not set(PINNED_VERSIONS) <= set(MODERN_PROTOCOL_VERSIONS):  # pragma: no cover
    raise ImportError("the installed mcp SDK does not serve the pinned protocol versions")


class McpStartError(Exception):
    """The MCP server could not start, for example because the port is taken."""


def allowed_hosts(port: int, extra: Sequence[str] = ()) -> frozenset[str]:
    """``Host`` values the server answers: loopback on ``port``, plus ``extra``.

    An extra name without a port, such as the Compose service name ``jotflow``,
    gets ``port``.
    """
    hosts = {f"{name}:{port}" for name in LOOPBACK_NAMES}
    for name in extra:
        hosts.add(name if name.rsplit(":", 1)[-1].isdigit() else f"{name}:{port}")
    return frozenset(hosts)


def _grant(ctx: ServerRequestContext[Any, Any]) -> Grant:
    request = ctx.request
    grant = getattr(getattr(request, "state", None), GRANT_KEY, None)
    if not isinstance(grant, Grant):
        # The gate admits nothing without a grant, so this is unreachable
        # unless the app is mounted without it.
        raise MCPError(INVALID_PARAMS, "no grant for this request")
    return grant


def build_server(tools: Sequence[ToolSpec] = TOOLS) -> Server[Any]:
    """The MCP server with exactly ``tools``, which are fixed for its lifetime."""
    registry = checked_registry(tools)
    # Each run kind's list is fixed at startup, so a run's tools never change.
    listings = {
        kind: ListToolsResult(
            tools=[
                Tool(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_schema(),
                    output_schema=t.output_schema(),
                )
                for t in registry.values()
                if kind in t.kinds
            ]
        )
        for kind in RunKind
    }

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return listings[_grant(ctx).kind]

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult:
        grant = _grant(ctx)
        spec = registry.get(params.name)
        if spec is None or grant.kind not in spec.kinds:
            # Out-of-kind looks the same as unknown, so a run cannot probe
            # which tools other kinds have.
            raise MCPError(INVALID_PARAMS, f"unknown tool {params.name!r}")
        try:
            arguments = spec.input_model.model_validate(params.arguments or {})
        except ValidationError as exc:
            # The agent's own arguments, so naming the fields is fine; the
            # audit log never sees them.
            fields = ", ".join(".".join(str(p) for p in e["loc"]) or "-" for e in exc.errors())
            raise MCPError(INVALID_PARAMS, f"invalid arguments: {fields}") from None
        try:
            result = await spec.handler(grant, arguments)
        except ToolError as refused:
            return CallToolResult(content=[TextContent(text=refused.reason)], is_error=True)
        return CallToolResult(
            content=[TextContent(text=result.model_dump_json())],
            structured_content=result.model_dump(mode="json"),
        )

    return Server(
        "jotflow",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def build_app(
    *,
    port: int,
    extra_hosts: Sequence[str] = (),
    tokens: TokenCheck | None = None,
    tools: Sequence[ToolSpec] = TOOLS,
) -> Gate:
    """The gated ASGI app. ``tokens`` defaults to refusing everything (#16)."""
    hosts = allowed_hosts(port, extra_hosts)
    sdk_app = build_server(tools).streamable_http_app(
        streamable_http_path=PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_BODY_BYTES,
        # A second layer behind the gate, which is stricter about Origin.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(hosts),
            allowed_origins=[],
        ),
    )
    return Gate(
        sdk_app,
        allowed_hosts=hosts,
        versions=PINNED_VERSIONS,
        tokens=tokens or RejectAllTokens(),
        max_body=MAX_BODY_BYTES,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


class _QuietServer(uvicorn.Server):
    """uvicorn without its own signal handlers; ``jotflow run`` owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def bound_socket(host: str = "127.0.0.1") -> socket.socket:
    """A TCP socket bound to a free port on ``host``, not yet listening (#184).

    Hand it to :class:`McpServer` as ``sock`` so the port is never released
    between choosing it and serving, where another process could take it. Held
    open without a server, it is a port nobody can listen on: connecting is
    refused.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
    except OSError:
        sock.close()
        raise
    return sock


@dataclass
class McpServer:
    """Runs the app with uvicorn in the current event loop.

    With ``sock``, it serves on that already-bound socket, which then belongs to
    the server; ``port`` must be the socket's port.
    """

    app: ASGIApp
    host: str
    port: int
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS
    sock: socket.socket | None = None
    _server: uvicorn.Server | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            lifespan="on",
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
            limit_concurrency=32,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=5,
            h11_max_incomplete_event_size=16 * 1024,
        )
        server = self._server = _QuietServer(config)

        async def serve() -> None:
            # uvicorn calls sys.exit() on a bind error, which asyncio would
            # re-raise out of the event loop; turn it into ours here.
            try:
                await server.serve(sockets=[self.sock] if self.sock is not None else None)
            except (SystemExit, OSError) as exc:
                raise McpStartError(
                    f"could not listen on {self.host}:{self.port}; is the port in use?"
                ) from exc

        self._task = asyncio.create_task(serve())
        deadline = asyncio.get_running_loop().time() + self.startup_timeout
        while not server.started:
            if self._task.done():
                self._close_handed_socket()
                self._task.result()  # raises McpStartError
                raise McpStartError(f"MCP server on {self.host}:{self.port} stopped at startup")
            if asyncio.get_running_loop().time() > deadline:
                # A hung lifespan never sees should_exit, so cancel instead.
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, McpStartError):
                    await self._task
                self._server = None
                self._task = None
                self._close_handed_socket()
                raise McpStartError(
                    f"MCP server on {self.host}:{self.port} did not start within "
                    f"{self.startup_timeout:g} seconds"
                )
            await asyncio.sleep(0.01)
        log.info("MCP server listening on %s:%d%s", self.host, self.port, PATH)

    def _close_handed_socket(self) -> None:
        """A start that failed may leave the socket it was handed open: close it (#263)."""
        if self.sock is not None:
            self.sock.close()  # closing one uvicorn already closed is harmless

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._server = None
        self._task = None
