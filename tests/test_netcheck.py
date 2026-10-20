"""Secrets go only to private addresses (#234): the check, the runner client, the
startup check, and the runner's own check of JotFlow's MCP host."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx2
import pytest

from jotflow import netcheck
from jotflow.adapters.runner import LaunchRefused, LaunchRequest, Runner, RunnerConfig
from jotflow.models import RUN_KINDS, RunKind
from jotflow.netcheck import AddressRefused, all_private, checked_address, is_private
from jotflow.orchestrator.launch import (
    CONNECT_TIMEOUT_SECONDS,
    RunnerClient,
    RunnerEndpoint,
    RunnerError,
)
from jotflow.orchestrator.service import RunKinds, check_runners

SECRET = "s" * 40
EXEC_SECRET = "e" * 40
TOKEN = "jfr_" + "t" * 43


class FakeResolver:
    """Answers from a table, and counts lookups. A missing name doesn't resolve."""

    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = table
        self.lookups: list[str] = []

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        self.lookups.append(host)
        if host not in self.table:
            raise OSError("name does not resolve")
        return self.table[host]


# The ranges


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "100.64.0.1",  # shared address space, where Tailscale puts nodes
        "100.127.255.254",
        "127.0.0.1",
        "169.254.10.10",
        "::1",
        "fd12:3456::1",
        "fe80::1",
        "fe80::1%eth0",
        "::ffff:10.1.2.3",  # IPv4-mapped is unwrapped
    ],
)
def test_private_addresses(address: str) -> None:
    assert is_private(address)


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "172.32.0.1",
        "100.128.0.1",
        "198.18.0.1",  # benchmark space, which ipaddress calls private
        "192.0.2.1",  # documentation space, also "private" to ipaddress
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
        "0.0.0.0",
    ],
)
def test_public_addresses(address: str) -> None:
    assert not is_private(address)


def test_one_public_answer_refuses_the_whole_lookup() -> None:
    assert all_private(["172.20.0.2", "10.0.0.3"])
    assert not all_private(["172.20.0.2", "93.184.216.34"])
    assert not all_private([])


async def test_the_checked_address_is_the_first_answer_unwrapped() -> None:
    resolver = FakeResolver({"agent": ["::ffff:172.20.0.2", "172.20.0.9"]})
    assert await checked_address(resolver, "agent", 8766, allow_public=False, opt_in="X") == (
        "172.20.0.2"
    )


async def test_a_literal_address_is_checked_without_a_lookup() -> None:
    resolver = FakeResolver({})
    assert await checked_address(resolver, "127.0.0.1", 1, allow_public=False, opt_in="X") == (
        "127.0.0.1"
    )
    assert await checked_address(resolver, "[::1]", 1, allow_public=False, opt_in="X") == "::1"
    with pytest.raises(AddressRefused, match="public address"):
        await checked_address(resolver, "8.8.8.8", 1, allow_public=False, opt_in="X")
    assert resolver.lookups == []


async def test_refusals_name_the_host_and_the_opt_in_but_never_the_address() -> None:
    resolver = FakeResolver({"agent-exec": ["93.184.216.34"]})
    with pytest.raises(AddressRefused) as public:
        await checked_address(
            resolver, "agent-exec", 8766, allow_public=False, opt_in="JOTFLOW_RUNNER_ALLOW_PUBLIC"
        )
    assert not public.value.unresolved
    assert "agent-exec resolves to a public address" in str(public.value)
    assert "JOTFLOW_RUNNER_ALLOW_PUBLIC=1" in str(public.value)
    assert "93.184" not in str(public.value)
    with pytest.raises(AddressRefused) as missing:
        await checked_address(resolver, "agent", 8766, allow_public=False, opt_in="X")
    assert missing.value.unresolved
    assert "agent does not resolve; is that service running?" in str(missing.value)


async def test_the_opt_in_allows_a_public_address() -> None:
    resolver = FakeResolver({"runner.example": ["93.184.216.34"]})
    assert (
        await checked_address(resolver, "runner.example", 8766, allow_public=True, opt_in="X")
        == "93.184.216.34"
    )


async def test_the_system_resolver_resolves_localhost() -> None:
    assert all_private(await netcheck.SystemResolver().resolve("localhost", 80))


# The runner client


class Recorder:
    def __init__(self, status: int = 202, body: Any = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.body = body if body is not None else {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body)


def client(
    recorder: Recorder, resolver: FakeResolver, *, allow_public: bool = False
) -> RunnerClient:
    return RunnerClient(
        conversation=RunnerEndpoint("http://agent:8766", SECRET),
        execution=RunnerEndpoint("http://agent-exec:8766", EXEC_SECRET),
        transport=httpx2.MockTransport(recorder),
        resolver=resolver,
        allow_public=allow_public,
    )


async def test_a_public_answer_sends_nothing_on_any_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = Recorder()
    runner = client(recorder, FakeResolver({"agent-exec": ["93.184.216.34"]}))
    calls = [
        runner.info(RunKind.EXECUTION),
        runner.launch("run-1", RunKind.EXECUTION, TOKEN),
        runner.status("run-1", RunKind.EXECUTION),
        runner.stop("run-1", RunKind.EXECUTION),
    ]
    with caplog.at_level(logging.WARNING, logger="jotflow.launch"):
        for call in calls:
            with pytest.raises(RunnerError) as refused:
                await call
            assert refused.value.retry is None
            assert "agent-exec resolves to a public address" in str(refused.value)
    assert recorder.requests == []
    assert EXEC_SECRET not in caplog.text and TOKEN not in caplog.text


async def test_a_private_answer_is_dialled_directly_with_the_name_in_host() -> None:
    recorder = Recorder(200, {"preset": "claude-code", "kinds": ["execution"]})
    resolver = FakeResolver({"agent-exec": ["172.20.0.3"]})
    await client(recorder, resolver).info(RunKind.EXECUTION)
    (request,) = recorder.requests
    assert request.url.host == "172.20.0.3" and request.url.port == 8766
    assert request.headers["host"] == "agent-exec:8766"
    assert request.headers["authorization"] == f"Bearer {EXEC_SECRET}"
    assert resolver.lookups == ["agent-exec"]


async def test_an_ipv6_answer_is_dialled_too() -> None:
    recorder = Recorder(200, {"preset": "claude-code", "kinds": ["execution"]})
    await client(recorder, FakeResolver({"agent-exec": ["fd00::3"]})).info(RunKind.EXECUTION)
    (request,) = recorder.requests
    assert request.url.host == "fd00::3"
    assert request.headers["host"] == "agent-exec:8766"


async def test_a_name_that_does_not_resolve_may_be_retried() -> None:
    recorder = Recorder()
    with pytest.raises(RunnerError) as missing:
        await client(recorder, FakeResolver({})).info(RunKind.DRAFTING)
    assert missing.value.retry == "unresolved"
    assert recorder.requests == []


async def test_the_opt_in_sends_to_a_public_runner_and_names_it() -> None:
    recorder = Recorder(200, {"preset": "claude-code", "kinds": ["execution"]})
    runner = client(recorder, FakeResolver({"agent-exec": ["93.184.216.34"]}), allow_public=True)
    await runner.info(RunKind.EXECUTION)
    assert recorder.requests[0].url.host == "93.184.216.34"
    assert runner.public_hosts() == ["agent", "agent-exec"]
    assert client(recorder, FakeResolver({})).public_hosts() == []


def test_every_runner_call_has_a_short_connect_timeout() -> None:
    timeout = RunnerClient(conversation=None, execution=None)._http.timeout
    assert timeout.connect == CONNECT_TIMEOUT_SECONDS == 3.0


# The startup check


async def test_a_public_runner_is_turned_off_at_once_without_retries() -> None:
    recorder = Recorder()
    resolver = FakeResolver(
        {"agent": ["172.20.0.2"], "agent-exec": ["93.184.216.34"]},
    )
    recorder.status, recorder.body = (
        200,
        {"preset": "claude-code", "kinds": ["drafting", "question"]},
    )
    kinds = RunKinds(RUN_KINDS, ())
    result = await check_runners(
        client(recorder, resolver), kinds, "claude-code", attempts=10, delay=0.0
    )
    assert result.on == frozenset({RunKind.DRAFTING, RunKind.QUESTION})
    (reason,) = result.off
    assert "execution runs are off" in reason
    assert "agent-exec resolves to a public address" in reason
    assert resolver.lookups.count("agent-exec") == 1


async def test_a_runner_that_does_not_resolve_stops_within_the_budget() -> None:
    from jotflow.orchestrator import service

    resolver = FakeResolver({})
    kinds = RunKinds(frozenset({RunKind.EXECUTION}), ())
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await service.check_runners(
        client(Recorder(), resolver), kinds, "claude-code", attempts=10, delay=0.05
    )
    assert result.on == frozenset()
    assert "agent-exec does not resolve; is that service running?" in result.off[0]
    assert loop.time() - started < 2
    # With a 15 s budget and a 0.05 s delay that's every attempt; with a tiny budget,
    # far fewer.
    resolver.lookups.clear()
    await service._runner_mismatch(
        client(Recorder(), resolver),
        frozenset({RunKind.EXECUTION}),
        "claude-code",
        attempts=10,
        delay=0.05,
        unresolved_budget=0.12,
    )
    assert 1 <= len(resolver.lookups) <= 3


# The runner's own check of JotFlow's MCP host


def runner_config(mcp_url: str, **overrides: Any) -> RunnerConfig:
    fields: dict[str, Any] = {
        "secret": SECRET,
        "mcp_url": mcp_url,
        "command": ("true",),
        "read_token": "ghp_" + "r" * 36,
        "allowed_hosts": frozenset({"agent:8766"}),
    }
    fields.update(overrides)
    return RunnerConfig(**fields)


async def test_the_runner_refuses_a_run_when_jotflow_resolves_publicly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = Runner(
        runner_config("http://jotflow:8765/mcp"),
        environ={},
        resolver=FakeResolver({"jotflow": ["93.184.216.34"]}),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(LaunchRefused) as refused:
        await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    assert refused.value.status == 502
    assert "jotflow resolves to a public address" in refused.value.reason
    assert "JOTFLOW_MCP_ALLOW_PUBLIC=1" in refused.value.reason
    assert runner.get("run-1") is None
    assert TOKEN not in caplog.text


async def test_the_runner_refuses_a_run_when_jotflow_does_not_resolve() -> None:
    runner = Runner(runner_config("http://jotflow:8765/mcp"), environ={}, resolver=FakeResolver({}))
    with pytest.raises(LaunchRefused) as refused:
        await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    assert refused.value.status == 502
    assert "jotflow does not resolve" in refused.value.reason


async def test_the_runner_accepts_a_private_or_opted_in_mcp_host() -> None:
    private = Runner(
        runner_config("http://jotflow:8765/mcp"),
        environ={},
        resolver=FakeResolver({"jotflow": ["172.20.0.4"]}),
    )
    assert await private.check_mcp_host() == "172.20.0.4"
    public = Runner(
        runner_config("https://jotflow.example/mcp", mcp_allow_public=True),
        environ={},
        resolver=FakeResolver({"jotflow.example": ["93.184.216.34"]}),
    )
    assert await public.check_mcp_host() == "93.184.216.34"


def test_jotflow_maps_the_runners_refusal_to_a_fixed_reason() -> None:
    from jotflow.orchestrator.launch import _refused

    assert "doesn't resolve to a private address (502)" in str(_refused(502))


async def test_runner_startup_stops_on_a_public_mcp_host(
    capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    from jotflow.commands import runner as command

    config = runner_config("http://93.184.216.34:8765/mcp", workdir=tmp_path)
    settings = command.RunnerSettings.model_construct(runner_host="127.0.0.1", runner_port=1)
    assert await command.serve(settings, config) == command.EXIT_CONFIG
    assert "resolves to a public address" in capsys.readouterr().err


async def test_runner_startup_only_warns_while_jotflow_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Any
) -> None:
    from jotflow.commands import runner as command

    served: list[bool] = []

    async def serve(self: Any) -> None:
        served.append(True)

    async def missing(self: Any, host: str, port: int) -> Sequence[str]:
        raise OSError("name does not resolve")  # never the real resolver: it may be a wildcard

    monkeypatch.setattr(command.uvicorn.Server, "serve", serve)
    monkeypatch.setattr(netcheck.SystemResolver, "resolve", missing)
    config = runner_config("http://jotflow-not-up.invalid:8765/mcp", workdir=tmp_path)
    settings = command.RunnerSettings.model_construct(runner_host="127.0.0.1", runner_port=1)
    with caplog.at_level(logging.WARNING):
        assert await command.serve(settings, config) == 0
    assert served == [True]
    assert "jotflow-not-up.invalid does not resolve" in caplog.text
