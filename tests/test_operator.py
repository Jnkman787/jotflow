"""Admin-issued, read-only operator tokens (#58, D12).

Covers the gate's in-memory copy of the tokens, what an OPERATOR grant may
read, a walk of every registered tool, the audit line, the CLI, and the stdio
bridge a terminal MCP client runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from jotflow.cli import main
from jotflow.commands import operator as command
from jotflow.models import MessageRef, RunKind
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore, OperatorToken, SqliteStore, Store
from jotflow.whiteboard.operator_tokens import (
    OPERATOR_PREFIX,
    GateTokens,
    OperatorTokens,
    digest,
    grant_for,
)
from jotflow.whiteboard.read_tools import GetTopicInput, ListTopicsInput, read_tools
from jotflow.whiteboard.server import PATH, PINNED_VERSIONS, McpServer, build_app
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolError, ToolSpec, checked_registry
from jotflow.whiteboard.write_tools import write_tools

CH = "C0IDEAS01"
OTHER = "C0IDEAS02"
THIRD = "C0IDEAS03"
POST = "1760781600.000100"
TITLE = "Progress bar SECRET TOPIC TITLE"
POLICY_TEXT = f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"

[channels.{OTHER}]

[[channels.{OTHER}.repos]]
repo = "acme-co/api"
nickname = "api"
description = "The public API"

[channels.{THIRD}]

[[channels.{THIRD}.repos]]
repo = "acme-co/docs"
nickname = "docs"
description = "The docs site"
"""
POLICY = parse_policy(POLICY_TEXT)
READERS = {"list_topics", "get_topic"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Monotonic:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class CountingStore(MemoryStore):
    """Counts how often the token copy is reloaded from the store."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock=clock)
        self.loads = 0

    async def operator_tokens(self) -> list[OperatorToken]:
        self.loads += 1
        return await super().operator_tokens()


def record(
    name: str = "alex",
    channels: tuple[str, ...] = (CH,),
    *,
    now: datetime,
    days: int = 30,
) -> tuple[str, OperatorToken]:
    return command.new_token(name, channels, days, POLICY, now)


class NeverPosts:
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        raise AssertionError("an operator never posts to Slack")


def all_tools(store: Store, clock: Clock) -> tuple[ToolSpec, ...]:
    return (
        *read_tools(store, POLICY, clock=clock),
        *write_tools(store, POLICY, NeverPosts()),
    )


# The token copy the gate checks


async def test_a_token_is_checked_by_digest_and_grants_its_channels() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    token, made = record(channels=(CH, OTHER), now=clock.now)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    await tokens.reload()
    grant = tokens.check(token)
    assert grant == grant_for(made)
    assert grant is not None
    assert grant.kind is RunKind.OPERATOR
    assert grant.channels == {CH, OTHER}
    assert grant.thread_ts is None and grant.proposal_id is None
    assert tokens.check(token + "x") is None
    assert tokens.check("jfr_" + token[4:]) is None


async def test_only_the_digest_is_stored() -> None:
    clock = Clock()
    token, made = record(now=clock.now)
    assert token.startswith(OPERATOR_PREFIX)
    assert made.digest == digest(token)
    assert token not in repr(made)
    assert made.digest not in repr(made)


async def test_a_new_token_works_at_once_through_one_miss_reload() -> None:
    clock, mono = Clock(), Monotonic()
    store = CountingStore(clock)
    tokens = OperatorTokens(store, clock=clock, monotonic=mono)
    await tokens.reload()
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    assert tokens.check(token) is None
    assert await tokens.check_on_miss(token) == grant_for(made)


async def test_a_flood_of_bad_tokens_forces_at_most_one_reload_a_second() -> None:
    clock, mono = Clock(), Monotonic()
    store = CountingStore(clock)
    tokens = OperatorTokens(store, clock=clock, monotonic=mono)
    await tokens.reload()
    before = store.loads
    for i in range(200):
        assert await tokens.check_on_miss(f"{OPERATOR_PREFIX}bad{i}") is None
    assert store.loads == before + 1
    mono.now += 0.5
    await tokens.check_on_miss(f"{OPERATOR_PREFIX}bad")
    assert store.loads == before + 1
    mono.now += 0.6
    await tokens.check_on_miss(f"{OPERATOR_PREFIX}bad")
    assert store.loads == before + 2


async def test_a_run_token_never_triggers_an_operator_reload() -> None:
    clock, mono = Clock(), Monotonic()
    store = CountingStore(clock)
    tokens = OperatorTokens(store, clock=clock, monotonic=mono)
    gate = GateTokens(TokenRegistry(), tokens)
    await tokens.reload()
    before = store.loads
    assert gate.check("jfr_" + "a" * 43) is None
    assert await tokens.check_on_miss("jfr_" + "a" * 43) is None
    assert store.loads == before


async def test_a_revoked_token_stops_within_the_reload_interval() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    stop = asyncio.Event()
    loop = asyncio.create_task(tokens.run(stop, interval=0.02))
    try:
        await tokens.reload()
        assert tokens.check(token) is not None
        await store.revoke_operator_token("alex")
        await asyncio.sleep(0.1)
        assert tokens.check(token) is None
        # And a miss does not bring it back.
        assert await tokens.check_on_miss(token) is None
    finally:
        stop.set()
        await loop


async def test_an_expired_token_is_refused_even_between_reloads() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    token, made = record(now=clock.now, days=1)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    await tokens.reload()
    assert tokens.check(token) is not None
    clock.now += timedelta(days=1)
    assert tokens.check(token) is None
    await tokens.reload()
    assert tokens.check(token) is None


async def test_last_use_is_written_back_on_reload_and_at_stop() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    await tokens.reload()
    clock.now += timedelta(minutes=5)
    used_at = clock.now
    assert tokens.check(token) is not None
    [before] = await store.operator_tokens()
    assert before.last_used_at is None
    await tokens.reload()
    [after] = await store.operator_tokens()
    assert after.last_used_at == used_at


async def test_forget_is_final() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    await tokens.reload()
    tokens.forget()
    assert tokens.check(token) is None
    await tokens.reload()
    assert tokens.check(token) is None
    assert await tokens.check_on_miss(token) is None


# What an operator grant may read


def operator(*channels: str) -> Grant:
    return Grant("operator:t1", RunKind.OPERATOR, channels[0], channels=frozenset(channels))


def tools_by_name(store: MemoryStore, clock: Clock) -> dict[str, ToolSpec]:
    return checked_registry(read_tools(store, POLICY, clock=clock))


async def test_a_single_channel_token_lists_its_channel_without_naming_it() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    mine = await store.create_topic(CH, TITLE)
    await store.link_message(MessageRef(channel_id=CH, ts=POST), mine.topic.id)
    await store.create_topic(OTHER, "Elsewhere")
    spec = tools_by_name(store, clock)["list_topics"]
    index = await spec.handler(operator(CH), ListTopicsInput())
    assert index.channel_id == CH
    assert [t.id for t in index.topics] == [mine.topic.id]


async def test_a_multi_channel_token_must_name_one_of_its_channels() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    spec = tools_by_name(store, clock)["list_topics"]
    grant = operator(CH, OTHER)
    with pytest.raises(ToolError, match=f"name one of this token's channels: {CH}, {OTHER}"):
        await spec.handler(grant, ListTopicsInput())
    assert (await spec.handler(grant, ListTopicsInput(channel=OTHER))).channel_id == OTHER
    with pytest.raises(ToolError, match="name one of"):
        await spec.handler(grant, ListTopicsInput(channel=THIRD))


async def test_a_run_cannot_name_another_channel() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    spec = tools_by_name(store, clock)["list_topics"]
    run = Grant("r1", RunKind.QUESTION, CH, thread_ts=POST)
    with pytest.raises(ToolError, match=f"channel {OTHER} is not this run's channel"):
        await spec.handler(run, ListTopicsInput(channel=OTHER))


async def test_get_topic_reads_the_tokens_channels_only() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    mine = await store.create_topic(OTHER, TITLE)
    elsewhere = await store.create_topic(THIRD, "Elsewhere")
    spec = tools_by_name(store, clock)["get_topic"]
    grant = operator(CH, OTHER)
    detail = await spec.handler(grant, GetTopicInput(topic_id=mine.topic.id))
    assert detail.id == mine.topic.id
    with pytest.raises(ToolError, match="no topic"):
        await spec.handler(grant, GetTopicInput(topic_id=elsewhere.topic.id))


async def test_each_operator_call_is_logged_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    found = await store.create_topic(CH, TITLE)
    by_name = tools_by_name(store, clock)
    grant = operator(CH)
    with caplog.at_level(logging.INFO, logger="jotflow.operator.audit"):
        await by_name["list_topics"].handler(grant, ListTopicsInput())
        await by_name["get_topic"].handler(grant, GetTopicInput(topic_id=found.topic.id))
    lines = [r.getMessage() for r in caplog.records if r.name == "jotflow.operator.audit"]
    at = clock.now.isoformat()
    assert lines == [
        f"operator call token=t1 tool=list_topics channel={CH} at={at}",
        f"operator call token=t1 tool=get_topic channel={CH} at={at}",
    ]
    assert TITLE not in caplog.text
    assert found.topic.id not in caplog.text


async def test_a_runs_calls_are_not_in_the_operator_log(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    spec = tools_by_name(store, clock)["list_topics"]
    with caplog.at_level(logging.INFO, logger="jotflow.operator.audit"):
        await spec.handler(Grant("r1", RunKind.QUESTION, CH, thread_ts=POST), ListTopicsInput())
    assert not [r for r in caplog.records if r.name == "jotflow.operator.audit"]


# Over the real server: tools/list, and a walk of every registered tool


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@asynccontextmanager
async def real_server(
    store: MemoryStore, clock: Clock
) -> AsyncIterator[tuple[int, OperatorTokens]]:
    port = _free_port()
    operators = OperatorTokens(store, clock=clock)
    await operators.reload()
    app = build_app(
        port=port, tokens=GateTokens(TokenRegistry(), operators), tools=all_tools(store, clock)
    )
    server = McpServer(app, "127.0.0.1", port)
    await server.start()
    try:
        yield port, operators
    finally:
        await server.stop()


@asynccontextmanager
async def http_client(port: int, token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as http:
        transport = streamable_http_client(f"http://127.0.0.1:{port}{PATH}", http_client=http)
        async with Client(transport, mode=PINNED_VERSIONS[0]) as client:
            yield client


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in _leaves(sub)]
    return [error]


async def test_every_registered_tool_is_walked_against_an_operator_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    store = MemoryStore(clock=clock)
    found = await store.create_topic(CH, TITLE)
    token, made = record(now=clock.now)
    async with real_server(store, clock) as (port, _):
        # Created after the server's first load: a miss reload lets it in.
        await store.add_operator_token(made)
        async with http_client(port, token) as client:
            listed = {t.name for t in (await client.list_tools()).tools}
            assert listed == READERS
            arguments: dict[str, dict[str, Any]] = {
                "list_topics": {},
                "get_topic": {"topic_id": found.topic.id},
            }
            names = sorted(t.name for t in all_tools(store, clock))
            assert set(names) > READERS
            for name in names:
                if name in READERS:
                    result = await client.call_tool(name, arguments[name])
                    assert not result.is_error, name
                    continue
                with pytest.raises(MCPError, match=f"unknown tool '{name}'"):
                    await client.call_tool(name, {})
    # Nothing an operator did wrote anything.
    assert await store.thread_proposals(CH, POST) == []
    # The gate's audit names the token by id; the token and its digest never appear.
    assert f"run=operator:{made.id}" in caplog.text
    assert token not in caplog.text
    assert made.digest not in caplog.text
    assert TITLE not in caplog.text


async def test_a_revoked_operator_token_is_refused_by_the_server() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    async with real_server(store, clock) as (port, operators):
        await store.revoke_operator_token("alex")
        await operators.reload()
        with pytest.raises(BaseExceptionGroup) as refused:
            async with http_client(port, token) as client:
                await client.list_tools()
    [error] = _leaves(refused.value)
    assert "the bearer token is not valid" in str(error)


# The CLI


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("JOTFLOW_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    (tmp_path / "jotflow.toml").write_text(POLICY_TEXT, encoding="utf-8")
    return tmp_path


def stored(workspace: Path) -> list[OperatorToken]:
    async def read() -> list[OperatorToken]:
        store = await SqliteStore.open(workspace / "jotflow.db")
        try:
            return await store.operator_tokens()
        finally:
            await store.close()

    return asyncio.run(read())


def test_create_prints_the_token_once_and_stores_its_digest(
    workspace: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        code = main(["operator", "create", "--name", "alex", "--channel", CH, "--channel", OTHER])
    out = capsys.readouterr()
    assert code == 0
    token = out.out.strip()
    assert token.startswith(OPERATOR_PREFIX)
    assert token not in out.err
    [made] = stored(workspace)
    assert made.name == "alex"
    assert made.channels == (CH, OTHER)
    assert made.digest == digest(token)
    assert made.expires_at - made.created_at == timedelta(days=30)
    assert token not in caplog.text
    raw = (workspace / "jotflow.db").read_bytes()
    assert token.encode() not in raw


def test_days_is_bounded(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["operator", "create", "--name", "a", "--channel", CH, "--days", "365"]) == 0
    for days in ("0", "366", "-1"):
        assert main(["operator", "create", "--name", "b", "--channel", CH, "--days", days]) == 2
        assert "--days must be between 1 and 365" in capsys.readouterr().err
    [made] = stored(workspace)
    assert made.expires_at - made.created_at == timedelta(days=365)


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["--name", "alex", "--channel", "C0NOWHERE"], "channel C0NOWHERE is not in jotflow.toml"),
        (["--name", "Alex", "--channel", CH], "lowercase"),
        (["--name", "a b", "--channel", CH], "lowercase"),
    ],
)
def test_create_refuses_bad_arguments(
    workspace: Path, capsys: pytest.CaptureFixture[str], args: list[str], said: str
) -> None:
    assert main(["operator", "create", *args]) == 2
    assert said in capsys.readouterr().err
    assert not (workspace / "jotflow.db").exists() or stored(workspace) == []


def test_a_name_is_never_reused(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["operator", "create", "--name", "alex", "--channel", CH]) == 0
    assert main(["operator", "revoke", "alex"]) == 0
    assert main(["operator", "create", "--name", "alex", "--channel", CH]) == 2
    assert "already exists" in capsys.readouterr().err


def test_list_shows_status_and_last_use_but_no_secret(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["operator", "list"]) == 0
    assert capsys.readouterr().out.strip() == "no operator tokens"
    assert main(["operator", "create", "--name", "alex", "--channel", CH]) == 0
    assert main(["operator", "create", "--name", "sam", "--channel", OTHER]) == 0
    tokens = capsys.readouterr().out.split()
    assert main(["operator", "revoke", "sam"]) == 0
    capsys.readouterr()
    assert main(["operator", "list"]) == 0
    listed = capsys.readouterr().out
    lines = listed.splitlines()
    assert lines[0].split() == ["NAME", "ID", "STATUS", "CHANNELS", "EXPIRES", "LAST", "USED"]
    by_name = {line.split()[0]: line for line in lines[1:]}
    assert "active" in by_name["alex"] and CH in by_name["alex"]
    assert "revoked" in by_name["sam"]
    assert by_name["alex"].endswith("never")
    for token in tokens:
        assert token not in listed
        assert digest(token) not in listed


def test_list_says_expired_and_shows_last_use() -> None:
    now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    _, made = record(now=now - timedelta(days=40))
    used = made.created_at + timedelta(days=2)
    lines = command.listing([OperatorToken(**{**made.__dict__, "last_used_at": used})], now)
    assert "expired" in lines[1]
    assert lines[1].endswith(used.strftime("%Y-%m-%d %H:%M UTC"))


def test_revoking_an_unknown_name_is_refused(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["operator", "revoke", "nobody"]) == 2
    assert "no live operator token named nobody" in capsys.readouterr().err


# The stdio bridge


def test_connect_reads_the_token_from_the_environment_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(command.TOKEN_ENV, raising=False)
    assert main(["operator", "connect"]) == 2
    assert f"set {command.TOKEN_ENV}" in capsys.readouterr().err
    monkeypatch.setenv(command.TOKEN_ENV, "jfr_" + "a" * 43)
    assert main(["operator", "connect"]) == 2
    assert "does not hold an operator token" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["operator", "connect", "--token", "jfo_x"])


def test_the_bridge_only_ever_targets_loopback() -> None:
    assert command.bridge_url({}) == f"http://127.0.0.1:8765{PATH}"
    assert command.bridge_url({command.PORT_ENV: "9000"}) == f"http://127.0.0.1:9000{PATH}"
    for bad in ("0", "70000", "example.com", "8765/../x"):
        with pytest.raises(command.Refusal):
            command.bridge_url({command.PORT_ENV: bad})


def bridge_params(port: int, token: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "jotflow.cli", "operator", "connect"],
        env={command.TOKEN_ENV: token, command.PORT_ENV: str(port)},
    )


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_a_terminal_client_reads_topics_through_the_bridge(mode: str) -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    found = await store.create_topic(CH, TITLE)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    async with (
        real_server(store, clock) as (port, _),
        Client(stdio_client(bridge_params(port, token)), mode=mode) as client,
    ):
        listed = {t.name for t in (await client.list_tools()).tools}
        index = await client.call_tool("list_topics", {})
        detail = await client.call_tool("get_topic", {"topic_id": found.topic.id})
        with pytest.raises(MCPError, match="unknown tool 'propose_issue'"):
            await client.call_tool("propose_issue", {})
    assert listed == READERS
    assert index.structured_content is not None
    assert [t["id"] for t in index.structured_content["topics"]] == [found.topic.id]
    assert detail.structured_content is not None
    assert detail.structured_content["title"]["text"] == TITLE


async def test_the_bridge_exits_clearly_on_a_refused_token() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    async with real_server(store, clock) as (port, _):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "jotflow.cli",
            "operator",
            "connect",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                command.TOKEN_ENV: OPERATOR_PREFIX + "x" * 43,
                command.PORT_ENV: str(port),
            },
        )
        out, err = await asyncio.wait_for(process.communicate(b""), timeout=30)
    assert process.returncode == command.EXIT_BRIDGE
    assert out == b""
    assert b"the bearer token is not valid" in err
    assert b"x" * 43 not in err


async def test_the_bridge_exits_clearly_when_jotflow_is_down() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "jotflow.cli",
        "operator",
        "connect",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            command.TOKEN_ENV: OPERATOR_PREFIX + "x" * 43,
            command.PORT_ENV: str(_free_port()),
        },
    )
    out, err = await asyncio.wait_for(process.communicate(b""), timeout=30)
    assert process.returncode == command.EXIT_BRIDGE
    assert out == b""
    assert b"could not reach JotFlow's MCP endpoint" in err


class FlakyStore(MemoryStore):
    """Fails the next ``fails`` reads or writes of operator tokens."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock=clock)
        self.fails = 0

    def _maybe_fail(self) -> None:
        if self.fails:
            self.fails -= 1
            raise OSError("disk went away")

    async def operator_tokens(self) -> list[OperatorToken]:
        self._maybe_fail()
        return await super().operator_tokens()

    async def touch_operator_tokens(self, used: Any) -> None:
        self._maybe_fail()
        await super().touch_operator_tokens(used)


async def test_the_reload_loop_survives_a_store_error_and_flushes_at_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    store = FlakyStore(clock)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    await tokens.reload()
    store.fails = 1
    stop = asyncio.Event()
    loop = asyncio.create_task(tokens.run(stop, interval=0.01))
    await asyncio.sleep(0.05)
    assert "could not reload operator tokens" in caplog.text
    assert tokens.check(token) is not None
    stop.set()
    await loop
    [after] = await store.operator_tokens()
    assert after.last_used_at == clock.now


async def test_a_failed_flush_keeps_last_use_for_the_next_one() -> None:
    clock = Clock()
    store = FlakyStore(clock)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    tokens = OperatorTokens(store, clock=clock)
    await tokens.reload()
    assert tokens.check(token) is not None
    store.fails = 1
    with pytest.raises(OSError):
        await tokens.flush()
    await tokens.flush()
    [after] = await store.operator_tokens()
    assert after.last_used_at == clock.now


def test_operator_with_no_action_shows_its_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["operator"]) == 2
    err = capsys.readouterr().err
    assert "usage: jotflow operator" in err
    assert "choose create, list, revoke or connect" in err


async def test_the_bridge_forwards_the_servers_own_answers_in_process() -> None:
    clock = Clock()
    store = MemoryStore(clock=clock)
    found = await store.create_topic(CH, TITLE)
    token, made = record(now=clock.now)
    await store.add_operator_token(made)
    async with (
        real_server(store, clock) as (port, _),
        command.upstream_client(f"http://127.0.0.1:{port}{PATH}", token) as upstream,
        Client(command.build_bridge(upstream)) as client,
    ):
        listed = {t.name for t in (await client.list_tools()).tools}
        detail = await client.call_tool("get_topic", {"topic_id": found.topic.id})
        missing = await client.call_tool("get_topic", {"topic_id": "nope"})
    assert listed == READERS
    assert not detail.is_error
    assert missing.is_error


def test_connect_reports_a_down_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(command.TOKEN_ENV, OPERATOR_PREFIX + "x" * 43)
    monkeypatch.setenv(command.PORT_ENV, str(_free_port()))
    assert main(["operator", "connect"]) == command.EXIT_BRIDGE
    assert "could not reach JotFlow's MCP endpoint" in capsys.readouterr().err


def test_connect_reports_a_refusal_from_inside_a_task_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refused(url: str, token: str) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(_raise(MCPError(-32001, "the bearer token is not valid")))

    async def _raise(error: BaseException) -> None:
        raise error

    monkeypatch.setenv(command.TOKEN_ENV, OPERATOR_PREFIX + "x" * 43)
    monkeypatch.setattr(command, "serve_bridge", refused)
    assert main(["operator", "connect"]) == command.EXIT_BRIDGE
    assert "refused: the bearer token is not valid" in capsys.readouterr().err


def test_connect_exits_zero_when_the_client_hangs_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def done(url: str, token: str) -> None:
        return None

    monkeypatch.setenv(command.TOKEN_ENV, OPERATOR_PREFIX + "x" * 43)
    monkeypatch.setattr(command, "serve_bridge", done)
    assert main(["operator", "connect"]) == command.EXIT_OK
