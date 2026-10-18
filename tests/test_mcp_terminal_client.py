"""#13's last two boxes, against the real server over TCP.

1. A terminal MCP client, the SDK's own streamable-HTTP client, connects with an
   issued run token and lists exactly its run kind's tools from the token table.
2. Every tool's advertised inputSchema equals its Pydantic model's schema, and
   its outputSchema equals the model's serialization schema.

The full tool set is built the way ``jotflow run`` will build it (#55), from
read_tools and write_tools over a MemoryStore. Nothing reaches Slack: the
poster is a stub that is never called, since listing calls no tool.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from jotflow.models import RunKind
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.server import PATH, PINNED_VERSIONS, McpServer, build_app
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolSpec, checked_registry
from jotflow.whiteboard.write_tools import write_tools

CH = "C0IDEAS01"
POST = "1760781600.000100"
POLICY = parse_policy(
    f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"
"""
)

# The token table in docs/architecture.md, written out so a drift fails here.
TOKEN_TABLE: dict[RunKind, set[str]] = {
    RunKind.DRAFTING: {
        "get_work",
        "list_topics",
        "get_topic",
        "propose_issue",
        "propose_update",
        "link_message",
        "ask_channel",
    },
    RunKind.EXECUTION: {"get_work", "record_result"},
    RunKind.QUESTION: {"get_work", "list_topics", "get_topic", "ask_channel"},
}


class NeverPosts:
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        raise AssertionError("listing tools never posts to Slack")


def all_tools() -> tuple[ToolSpec, ...]:
    store = MemoryStore()
    return (*read_tools(store, POLICY), *write_tools(store, POLICY, NeverPosts()))


def grant_for(kind: RunKind) -> Grant:
    if kind is RunKind.EXECUTION:
        return Grant(f"run-{kind.value}", kind, CH, proposal_id="p1")
    return Grant(f"run-{kind.value}", kind, CH, thread_ts=POST)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@asynccontextmanager
async def real_server(tokens: TokenRegistry) -> AsyncIterator[str]:
    port = _free_port()
    server = McpServer(build_app(port=port, tokens=tokens, tools=all_tools()), "127.0.0.1", port)
    await server.start()
    try:
        yield f"http://127.0.0.1:{port}{PATH}"
    finally:
        await server.stop()


async def listed_by_the_sdk_client(url: str, token: str) -> list[Any]:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode=PINNED_VERSIONS[0]) as client:
            result = await client.list_tools()
    return list(result.tools)


def test_the_token_table_matches_the_registry() -> None:
    for kind, names in TOKEN_TABLE.items():
        assert {t.name for t in all_tools() if kind in t.kinds} == names, kind


@pytest.mark.parametrize("kind", list(RunKind))
async def test_a_terminal_client_lists_its_run_kinds_tools(kind: RunKind) -> None:
    tokens = TokenRegistry()
    token = tokens.issue(grant_for(kind), ttl=60)
    async with real_server(tokens) as url:
        tools = await listed_by_the_sdk_client(url, token)
    assert {t.name for t in tools} == TOKEN_TABLE[kind]


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in _leaves(sub)]
    return [error]


async def test_a_revoked_token_cannot_connect() -> None:
    tokens = TokenRegistry()
    grant = grant_for(RunKind.DRAFTING)
    token = tokens.issue(grant, ttl=60)
    tokens.revoke(grant.run_id)
    async with real_server(tokens) as url:
        with pytest.raises(BaseExceptionGroup) as refused:
            await listed_by_the_sdk_client(url, token)
    [error] = _leaves(refused.value)
    assert isinstance(error, MCPError)
    assert "the bearer token is not valid" in str(error)


async def test_every_advertised_schema_matches_its_pydantic_model() -> None:
    specs = checked_registry(all_tools())
    tokens = TokenRegistry()
    async with real_server(tokens) as url:
        seen: dict[str, Any] = {}
        for kind in RunKind:
            token = tokens.issue(grant_for(kind), ttl=60)
            for tool in await listed_by_the_sdk_client(url, token):
                seen[tool.name] = tool
    assert set(seen) == set(specs)
    for name, tool in seen.items():
        spec = specs[name]
        assert tool.input_schema == spec.input_model.model_json_schema(), name
        expected_output = (
            None
            if spec.output_model is None
            else spec.output_model.model_json_schema(mode="serialization")
        )
        assert tool.output_schema == expected_output, name
