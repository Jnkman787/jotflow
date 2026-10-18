"""The whiteboard MCP server: gate rules, protocol behaviour, tools, audit, lifecycle.

Requests go to the ASGI app in process. Only the lifecycle tests open a
socket, on loopback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from jotflow.models import RunKind
from jotflow.whiteboard.gate import Gate
from jotflow.whiteboard.server import (
    MAX_BODY_BYTES,
    PATH,
    PINNED_VERSIONS,
    McpServer,
    McpStartError,
    allowed_hosts,
    build_app,
)
from jotflow.whiteboard.tokens import MAX_TOKEN_LENGTH, Grant, RejectAllTokens, TokenRegistry
from jotflow.whiteboard.tools import TOOLS, RegistryError, ToolError, ToolSpec, checked_registry

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "run-token-for-tests"
GRANT = Grant(run_id="run-1", kind=RunKind.DRAFTING, channel_id="C0EXAMPLE1", thread_ts="1.000100")
SECRET = "the launch code is swordfish"


class OneToken:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def check(self, token: str) -> Grant | None:
        self.seen.append(token)
        return GRANT if token == TOKEN else None


class EchoIn(BaseModel):
    text: str
    times: int = 1


class EchoOut(BaseModel):
    text: str
    run_id: str


async def echo(grant: Grant, arguments: Any) -> BaseModel:
    assert isinstance(arguments, EchoIn)
    return EchoOut(text=arguments.text * arguments.times, run_id=grant.run_id)


DRAFTING = frozenset({RunKind.DRAFTING})
ECHO = ToolSpec(
    name="echo",
    description="Repeat text. For tests only.",
    input_model=EchoIn,
    handler=echo,
    kinds=DRAFTING,
)


@asynccontextmanager
async def client_for(
    *,
    tokens: Any = None,
    tools: Sequence[ToolSpec] = TOOLS,
    extra_hosts: Sequence[str] = (),
) -> AsyncIterator[httpx2.AsyncClient]:
    app = build_app(port=PORT, extra_hosts=extra_hosts, tokens=tokens, tools=tools)
    inner: Any = app.inner
    async with (
        inner.router.lifespan_context(inner),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE, timeout=10
        ) as http,
    ):
        yield http


def envelope(method: str, params: dict[str, Any] | None = None, *, id: int = 1) -> dict[str, Any]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": PINNED_VERSIONS[0],
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return {
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": {**(params or {}), "_meta": meta},
    }


def headers_for(method: str, name: str | None = None, **extra: str) -> dict[str, str]:
    headers = {
        "MCP-Protocol-Version": PINNED_VERSIONS[0],
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    headers.update(extra)
    return headers


async def post(
    http: httpx2.AsyncClient, method: str, params: dict[str, Any] | None = None, **headers: str
) -> httpx2.Response:
    name = (params or {}).get("name")
    return await http.post(
        PATH, json=envelope(method, params), headers={**headers_for(method, name), **headers}
    )


# The happy path, through the SDK.


async def test_discover_declares_only_tools_without_list_changed() -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await post(http, "server/discover")
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    result = response.json()["result"]
    assert result["supportedVersions"] == list(PINNED_VERSIONS)
    assert result["capabilities"] == {"tools": {"listChanged": False}}


async def test_the_sdk_client_connects_and_lists_no_tools() -> None:
    async with client_for(tokens=OneToken()) as http:
        http.headers["Authorization"] = f"Bearer {TOKEN}"
        transport = streamable_http_client(f"{BASE}{PATH}", http_client=http)
        async with Client(transport, mode=PINNED_VERSIONS[0]) as client:
            listed = await client.list_tools()
    assert listed.tools == []


async def test_the_registry_ships_empty_until_the_tools_land() -> None:
    assert TOOLS == ()


async def test_a_registered_tool_is_listed_with_its_pydantic_schema() -> None:
    async with client_for(tokens=OneToken(), tools=[ECHO]) as http:
        response = await post(http, "tools/list")
    [tool] = response.json()["result"]["tools"]
    assert tool["name"] == "echo"
    assert tool["description"] == "Repeat text. For tests only."
    assert tool["inputSchema"] == EchoIn.model_json_schema()


async def test_a_tool_call_gets_the_grant_and_validated_arguments() -> None:
    async with client_for(tokens=OneToken(), tools=[ECHO]) as http:
        response = await post(
            http, "tools/call", {"name": "echo", "arguments": {"text": "ab", "times": 2}}
        )
    result = response.json()["result"]
    assert result["structuredContent"] == {"text": "abab", "run_id": "run-1"}


async def test_invalid_arguments_name_the_field_not_the_value() -> None:
    async with client_for(tokens=OneToken(), tools=[ECHO]) as http:
        response = await post(
            http, "tools/call", {"name": "echo", "arguments": {"text": SECRET, "times": "x"}}
        )
    error = response.json()["error"]
    assert error["message"] == "invalid arguments: times"
    assert SECRET not in response.text


async def test_an_unknown_tool_is_an_error() -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await post(http, "tools/call", {"name": "nope", "arguments": {}})
    assert "unknown tool 'nope'" in response.json()["error"]["message"]


async def test_header_and_body_disagreeing_is_400_header_mismatch() -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await http.post(
            PATH, json=envelope("server/discover"), headers=headers_for("tools/list")
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


# The gate.


async def test_without_a_token_check_every_token_is_refused() -> None:
    assert RejectAllTokens().check(TOKEN) is None
    async with client_for() as http:
        response = await post(http, "server/discover")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Bearer a b",
        f"Bearer {'x' * (MAX_TOKEN_LENGTH + 1)}",
    ],
)
async def test_missing_or_malformed_tokens_are_401_and_never_checked(
    authorization: str | None,
) -> None:
    tokens = OneToken()
    async with client_for(tokens=tokens) as http:
        headers = headers_for("server/discover")
        if authorization is None:
            del headers["Authorization"]
        else:
            headers["Authorization"] = authorization
        response = await http.post(PATH, json=envelope("server/discover"), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert tokens.seen == []


async def test_a_wrong_token_is_401() -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await post(http, "server/discover", Authorization="Bearer someone-else")
    assert response.status_code == 401


@pytest.mark.parametrize("origin", [BASE, "http://evil.example", "null"])
async def test_any_origin_header_is_403(origin: str) -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await post(http, "server/discover", Origin=origin)
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1:9999", ""])
async def test_a_host_off_the_allowlist_is_403(host: str) -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await post(http, "server/discover", Host=host)
    assert response.status_code == 403


async def test_a_configured_host_such_as_the_compose_service_is_allowed() -> None:
    async with client_for(tokens=OneToken(), extra_hosts=["jotflow"]) as http:
        response = await post(http, "server/discover", Host=f"jotflow:{PORT}")
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "OPTIONS"])
async def test_only_post_is_served(method: str) -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await http.request(method, PATH, headers=headers_for("server/discover"))
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


async def test_a_query_string_is_400_so_tokens_never_ride_in_urls() -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await http.post(
            f"{PATH}?access_token={TOKEN}",
            json=envelope("server/discover"),
            headers=headers_for("server/discover"),
        )
    assert response.status_code == 400


@pytest.mark.parametrize("version", [None, "2025-11-25", "2025-03-26", "2099-01-01"])
async def test_unpinned_versions_get_the_unsupported_version_error(version: str | None) -> None:
    async with client_for(tokens=OneToken()) as http:
        headers = headers_for("initialize")
        if version is None:
            del headers["MCP-Protocol-Version"]
        else:
            headers["MCP-Protocol-Version"] = version
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "old", "version": "1"},
            },
        }
        response = await http.post(PATH, json=body, headers=headers)
    assert response.status_code == 400
    assert "mcp-session-id" not in response.headers
    error = response.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": list(PINNED_VERSIONS), "requested": version or ""}


async def test_a_declared_body_over_the_limit_is_413() -> None:
    async with client_for(tokens=OneToken()) as http:
        response = await http.post(
            PATH, content=b" " * (MAX_BODY_BYTES + 1), headers=headers_for("server/discover")
        )
    assert response.status_code == 413


async def test_a_streamed_body_over_the_limit_is_413() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b" " * 1024

    async with client_for(tokens=OneToken()) as http:
        response = await http.post(PATH, content=chunks(), headers=headers_for("server/discover"))
    assert response.status_code == 413


async def test_a_non_numeric_content_length_is_400() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        raise AssertionError("the gate must not pass this on")

    gate = Gate(
        app,
        allowed_hosts={f"127.0.0.1:{PORT}"},
        versions=PINNED_VERSIONS,
        tokens=OneToken(),
        max_body=10,
        timeout=1,
    )
    sent: list[Any] = []
    headers = [
        (b"host", f"127.0.0.1:{PORT}".encode()),
        (b"content-length", b"ten"),
        (b"mcp-protocol-version", PINNED_VERSIONS[0].encode()),
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": PATH,
        "headers": headers,
        "query_string": b"",
    }

    async def send(message: Any) -> None:
        sent.append(message)

    await gate(scope, _no_body, send)
    assert sent[0]["status"] == 400


async def _no_body() -> Any:
    return {"type": "http.request", "body": b"", "more_body": False}


# Timeouts, against a stand-in app.


def _gate(app: Any, timeout: float = 0.05) -> Gate:
    return Gate(
        app,
        allowed_hosts={f"127.0.0.1:{PORT}"},
        versions=PINNED_VERSIONS,
        tokens=OneToken(),
        max_body=1024,
        timeout=timeout,
    )


def _scope() -> dict[str, Any]:
    headers = [
        (b"host", f"127.0.0.1:{PORT}".encode()),
        (b"mcp-protocol-version", PINNED_VERSIONS[0].encode()),
        (b"mcp-method", b"tools/call"),
        (b"mcp-name", b"echo"),
        (b"authorization", f"Bearer {TOKEN}".encode()),
    ]
    return {"type": "http", "method": "POST", "path": PATH, "headers": headers, "query_string": b""}


async def test_a_slow_request_gets_504(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jotflow.mcp.audit")

    async def slow(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(10)

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    await _gate(slow)(_scope(), _no_body, send)
    assert sent[0]["status"] == 504
    assert "status=504 outcome=timeout" in caplog.text


async def test_a_timeout_after_the_response_started_sends_nothing_more() -> None:
    async def stalls(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(10)

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    await _gate(stalls)(_scope(), _no_body, send)
    assert [m["type"] for m in sent] == ["http.response.start"]


async def test_lifespan_passes_through_the_gate() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await _gate(app)({"type": "lifespan"}, _no_body, _discard)
    assert seen == ["lifespan"]


async def _discard(message: Any) -> None:
    return None


# The audit log.


async def test_every_request_is_audited_without_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jotflow.mcp.audit")
    async with client_for(tokens=OneToken(), tools=[ECHO]) as http:
        await post(http, "tools/call", {"name": "echo", "arguments": {"text": SECRET}})
        await post(http, "server/discover", Authorization="Bearer wrong")
        await post(http, "server/discover", Origin="http://evil.example")
    lines = [r.getMessage() for r in caplog.records if r.name == "jotflow.mcp.audit"]
    assert len(lines) == 3
    assert lines[0].startswith(
        "mcp method=tools/call tool=echo run=run-1 status=200 outcome=served"
    )
    assert lines[1].startswith(
        "mcp method=server/discover tool=- run=- status=401 outcome=bad token"
    )
    assert lines[2].startswith("mcp method=server/discover tool=- run=- status=403 outcome=origin")
    joined = "\n".join(lines)
    assert SECRET not in joined
    assert TOKEN not in joined
    assert "wrong" not in joined


async def test_unsafe_header_values_are_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jotflow.mcp.audit")
    async with client_for(tokens=OneToken()) as http:
        await http.post(
            PATH,
            json=envelope("tools/call", {"name": "x"}),
            headers=headers_for("tools/call", "x run=forged status=200"),
        )
    [line] = [r.getMessage() for r in caplog.records if r.name == "jotflow.mcp.audit"]
    assert "forged" not in line
    assert "tool=?" in line


# Registry and settings.


async def _noop(grant: Grant, arguments: Any) -> BaseModel:  # pragma: no cover
    return EchoOut(text="", run_id=grant.run_id)


@pytest.mark.parametrize(
    ("tools", "problem"),
    [
        ([ToolSpec("bad name", "d", EchoIn, _noop, DRAFTING)], "breaks the MCP naming rule"),
        (
            [
                ToolSpec("a", "d", EchoIn, _noop, DRAFTING),
                ToolSpec("a", "d", EchoIn, _noop, DRAFTING),
            ],
            "registered twice",
        ),
        ([ToolSpec("a", "  ", EchoIn, _noop, DRAFTING)], "has no description"),
        ([ToolSpec("a", "d", EchoIn, _noop)], "names no run kind"),
    ],
)
def test_a_malformed_registry_fails_at_startup(tools: list[ToolSpec], problem: str) -> None:
    with pytest.raises(RegistryError, match=problem):
        checked_registry(tools)


def test_allowed_hosts() -> None:
    assert allowed_hosts(8765) == {"127.0.0.1:8765", "localhost:8765", "[::1]:8765"}
    assert {"jotflow:8765", "agent-net:9000"} <= allowed_hosts(8765, ["jotflow", "agent-net:9000"])


def test_settings_parse_extra_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from jotflow.config import Settings

    monkeypatch.setenv("JOTFLOW_MCP_ALLOWED_HOSTS", "jotflow, jotflow:9000")
    settings = Settings(slack_bot_token="x", slack_app_token="y", _env_file=None)  # type: ignore[call-arg]
    assert settings.mcp_allowed_hosts == ("jotflow", "jotflow:9000")
    monkeypatch.setenv("JOTFLOW_MCP_ALLOWED_HOSTS", "bad host")
    with pytest.raises(ValidationError, match="host name"):
        Settings(slack_bot_token="x", slack_app_token="y", _env_file=None)  # type: ignore[call-arg]


# Lifecycle, on loopback.


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_the_server_starts_answers_and_stops_without_touching_signals() -> None:
    port = _free_port()
    before = signal.getsignal(signal.SIGTERM)
    server = McpServer(build_app(port=port, tokens=OneToken()), "127.0.0.1", port)
    await server.start()
    try:
        assert signal.getsignal(signal.SIGTERM) == before
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            response = await http.post(
                PATH,
                content=json.dumps(envelope("server/discover")),
                headers=headers_for("server/discover"),
            )
        assert response.status_code == 200
    finally:
        await server.stop()
    await server.stop()  # stopping twice is harmless
    with socket.socket() as again:
        again.bind(("127.0.0.1", port))  # the port is free again


async def test_a_taken_port_is_a_clear_start_error() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        server = McpServer(build_app(port=port), "127.0.0.1", port)
        with pytest.raises(McpStartError, match=f"could not listen on 127.0.0.1:{port}"):
            await server.start()


async def test_a_startup_that_hangs_is_a_clear_start_error() -> None:
    async def hangs(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await receive()  # lifespan.startup arrives, and is never answered
            await asyncio.sleep(10)

    port = _free_port()
    server = McpServer(hangs, "127.0.0.1", port, startup_timeout=0.2)
    with pytest.raises(McpStartError, match=r"did not start within 0\.2 seconds"):
        await server.start()


# Per-run tokens through the real endpoint (#16).

EXECUTION_GRANT = Grant(
    run_id="run-exec", kind=RunKind.EXECUTION, channel_id="C0EXAMPLE1", proposal_id="prop-1"
)


async def test_issued_tokens_admit_their_own_run_only() -> None:
    registry = TokenRegistry()
    drafting = registry.issue(GRANT, 60)
    other = registry.issue(
        Grant(run_id="run-2", kind=RunKind.DRAFTING, channel_id="C0EX", thread_ts="2.000100"), 60
    )
    async with client_for(tokens=registry, tools=[ECHO]) as http:
        mine = await post(
            http,
            "tools/call",
            {"name": "echo", "arguments": {"text": "a"}},
            Authorization=f"Bearer {drafting}",
        )
        theirs = await post(
            http,
            "tools/call",
            {"name": "echo", "arguments": {"text": "a"}},
            Authorization=f"Bearer {other}",
        )
    assert mine.json()["result"]["structuredContent"]["run_id"] == "run-1"
    assert theirs.json()["result"]["structuredContent"]["run_id"] == "run-2"


async def test_a_run_lists_and_calls_only_its_kinds_tools() -> None:
    registry = TokenRegistry()
    execution = registry.issue(EXECUTION_GRANT, 60)
    async with client_for(tokens=registry, tools=[ECHO]) as http:
        listed = await post(http, "tools/list", Authorization=f"Bearer {execution}")
        called = await post(
            http,
            "tools/call",
            {"name": "echo", "arguments": {"text": "a"}},
            Authorization=f"Bearer {execution}",
        )
        unknown = await post(
            http,
            "tools/call",
            {"name": "nope", "arguments": {}},
            Authorization=f"Bearer {execution}",
        )
    assert listed.json()["result"]["tools"] == []
    # An out-of-kind tool is indistinguishable from one that does not exist.
    assert called.json()["error"] == {**unknown.json()["error"], "message": "unknown tool 'echo'"}


async def test_a_revoked_or_expired_token_is_401(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jotflow.mcp.audit")
    now = [0.0]
    registry = TokenRegistry(clock=lambda: now[0])
    revoked = registry.issue(GRANT, 60)
    expiring = registry.issue(EXECUTION_GRANT, 60)
    registry.revoke(GRANT.run_id)
    now[0] = 61
    async with client_for(tokens=registry) as http:
        first = await post(http, "server/discover", Authorization=f"Bearer {revoked}")
        second = await post(http, "server/discover", Authorization=f"Bearer {expiring}")
    assert (first.status_code, second.status_code) == (401, 401)
    log = "\n".join(r.getMessage() for r in caplog.records)
    for token in (revoked, expiring):
        assert token not in log
        assert token not in first.text + second.text


# #14: tool errors and output schemas


async def failing(grant: Grant, arguments: Any) -> BaseModel:
    raise ToolError("run run-1 is finished")


FAILING = ToolSpec(
    name="failing",
    description="Always fails. For tests only.",
    input_model=EchoIn,
    handler=failing,
    kinds=DRAFTING,
)
TYPED = ToolSpec(
    name="typed",
    description="Echo with an output schema. For tests only.",
    input_model=EchoIn,
    output_model=EchoOut,
    handler=echo,
    kinds=DRAFTING,
)


async def test_a_tool_error_is_a_result_the_model_sees() -> None:
    async with client_for(tokens=OneToken(), tools=[FAILING]) as http:
        response = await post(http, "tools/call", {"name": "failing", "arguments": {"text": "x"}})
    body = response.json()
    assert "error" not in body
    assert body["result"]["isError"] is True
    assert body["result"]["content"] == [{"type": "text", "text": "run run-1 is finished"}]


async def test_an_output_model_is_advertised_as_the_output_schema() -> None:
    async with client_for(tokens=OneToken(), tools=[TYPED, ECHO]) as http:
        response = await post(http, "tools/list")
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    assert tools["typed"]["outputSchema"] == EchoOut.model_json_schema()
    assert "outputSchema" not in tools["echo"] or tools["echo"]["outputSchema"] is None
