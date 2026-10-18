"""The gate in front of the MCP endpoint.

An ASGI middleware that applies JotFlow's transport rules before any SDK code
sees a request, and writes one audit line per request. The rules come from the
2026-07-28 MCP specification and the review notes on #13 and #16:

1. Any ``Origin`` header: 403. Agents never send one; a browser always does.
2. A ``Host`` not on the allowlist: 403, against DNS rebinding.
3. Any method but POST: 405. The modern transport has no GET stream and no
   DELETE, because it has no sessions.
4. Any query string: 400, so a token can never travel in a URL.
5. A missing or unpinned ``MCP-Protocol-Version``: 400 with the JSON-RPC
   unsupported-version error. This also shuts the SDK's handshake-era path,
   which would otherwise mint ``Mcp-Session-Id`` values.
6. A declared body over the limit: 413.
7. No valid bearer token: 401.
8. Otherwise the SDK serves it within the request timeout, or 504.

The audit line names the method, the tool, the run, the status, the outcome,
and the duration. It never includes arguments, bodies, or tokens, and the
client-supplied header values it does include are reduced to a safe charset.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Collection
from typing import Any

import anyio
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jotflow.whiteboard.tokens import MAX_TOKEN_LENGTH, Grant, TokenCheck

audit = logging.getLogger("jotflow.mcp.audit")

GRANT_KEY = "jotflow_grant"
UNSUPPORTED_PROTOCOL_VERSION = -32022
INVALID_REQUEST = -32600

_SAFE = re.compile(r"[A-Za-z0-9._/:-]{1,64}")


def _safe(value: str | None) -> str:
    """A header value fit for a log line, or ``?``."""
    if value is None:
        return "-"
    return value if _SAFE.fullmatch(value) else "?"


class _Rejection(Exception):
    def __init__(
        self,
        status: int,
        reason: str,
        *,
        code: int = INVALID_REQUEST,
        message: str,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.code = code
        self.message = message
        self.data = data
        self.headers = headers or {}


async def _send_json(send: Send, status: int, payload: Any, headers: dict[str, str]) -> None:
    body = json.dumps(payload).encode()
    raw = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *((k.lower().encode(), v.encode()) for k, v in headers.items()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


def _error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    # No id: the gate answers before reading the body, as the spec allows.
    return {"jsonrpc": "2.0", "id": None, "error": error}


class Gate:
    """The ASGI middleware. See the module docstring for the rules."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Collection[str],
        versions: Collection[str],
        tokens: TokenCheck,
        max_body: int,
        timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._hosts = frozenset(h.lower() for h in allowed_hosts)
        self._versions = tuple(versions)
        self._tokens = tokens
        self._max_body = max_body
        self._timeout = timeout
        self._clock = clock

    @property
    def inner(self) -> ASGIApp:
        """The app behind the gate, for entering its lifespan in tests."""
        return self._app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = self._clock()
        headers = Headers(scope=scope)
        status: list[int] = []

        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        grant: Grant | None = None
        try:
            grant = self._admit(scope, headers)
        except _Rejection as rejected:
            await _send_json(
                recording_send,
                rejected.status,
                _error(rejected.code, rejected.message, rejected.data),
                rejected.headers,
            )
            self._audit(headers, None, rejected.status, rejected.reason, started)
            return

        scope.setdefault("state", {})[GRANT_KEY] = grant
        outcome = "served"
        with anyio.move_on_after(self._timeout) as deadline:
            await self._app(scope, receive, recording_send)
        if deadline.cancelled_caught:
            outcome = "timeout"
            if not status:
                await _send_json(
                    recording_send, 504, _error(INVALID_REQUEST, "request timed out"), {}
                )
        self._audit(headers, grant, status[0] if status else 0, outcome, started)

    def _admit(self, scope: Scope, headers: Headers) -> Grant:
        if "origin" in headers:
            raise _Rejection(403, "origin", message="requests with an Origin header are refused")
        if headers.get("host", "").lower() not in self._hosts:
            raise _Rejection(403, "host", message="Host is not allowed")
        if scope["method"] != "POST":
            raise _Rejection(
                405, "method", message="only POST is served", headers={"Allow": "POST"}
            )
        if scope.get("query_string"):
            raise _Rejection(400, "query", message="query strings are not accepted")
        version = headers.get("mcp-protocol-version")
        if version not in self._versions:
            raise _Rejection(
                400,
                "version",
                code=UNSUPPORTED_PROTOCOL_VERSION,
                message="unsupported protocol version",
                data={"supported": list(self._versions), "requested": version or ""},
            )
        length = headers.get("content-length")
        if length is not None:
            if not length.isdigit():
                raise _Rejection(400, "length", message="Content-Length is not a number")
            if int(length) > self._max_body:
                raise _Rejection(413, "size", message="request body is too large")
        return self._authenticate(headers)

    def _authenticate(self, headers: Headers) -> Grant:
        value = headers.get("authorization")
        challenge = {"WWW-Authenticate": "Bearer"}
        if value is None:
            raise _Rejection(
                401, "no token", message="a bearer token is required", headers=challenge
            )
        scheme, _, token = value.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not token
            or len(token) > MAX_TOKEN_LENGTH
            or any(c.isspace() for c in token)
        ):
            raise _Rejection(401, "bad token", message="malformed bearer token", headers=challenge)
        grant = self._tokens.check(token)
        if grant is None:
            raise _Rejection(
                401,
                "bad token",
                message="the bearer token is not valid",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        return grant

    def _audit(
        self, headers: Headers, grant: Grant | None, status: int, outcome: str, started: float
    ) -> None:
        method = headers.get("mcp-method")
        tool = headers.get("mcp-name") if method == "tools/call" else None
        audit.info(
            "mcp method=%s tool=%s run=%s status=%d outcome=%s ms=%d",
            _safe(method),
            _safe(tool),
            grant.run_id if grant else "-",
            status,
            outcome,
            round((self._clock() - started) * 1000),
        )
