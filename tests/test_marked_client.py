"""JotFlow's side of the --from-github runner calls (#256): strict, capped, re-cleaned."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from jotflow.orchestrator.launch import RunnerClient, RunnerEndpoint, RunnerError
from tests.test_verify_client import SECRET

GOOD = {
    "number": 42,
    "state": "open",
    "title": "Progress bar",
    "author": "jotflow-bot",
    "markers": ["abc123"],
    "sources": [{"channel_id": "C0IDEAS01", "ts": "1760781660.000100", "thread_ts": None}],
}


def client(status: int, body: Any) -> tuple[RunnerClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if isinstance(body, bytes):
            return httpx2.Response(status, content=body)
        return httpx2.Response(status, json=body)

    return (
        RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", SECRET),
            execution=RunnerEndpoint("http://agent-exec:8766", "e" * 40),
            transport=httpx2.MockTransport(handle),
        ),
        seen,
    )


async def test_marked_issues_are_asked_with_a_body_and_read_strictly() -> None:
    runner, seen = client(200, {"issues": [GOOD], "truncated": False})
    found = await runner.marked_issues("acme-co", "web")
    assert [i.number for i in found.issues] == [42]
    [request] = seen
    assert request.method == "POST" and request.url.path == "/repos/jotflow-issues"
    assert request.headers["host"] == "agent:8766" and not request.url.query
    assert json.loads(request.content) == {"owner": "acme-co", "name": "web", "since": None}


@pytest.mark.parametrize(
    "issue",
    [
        {**GOOD, "body": "the whole issue text"},  # an extra field
        {**GOOD, "state": "merged"},
        {**GOOD, "number": 0},
        {**GOOD, "author": "not a login!"},
        {**GOOD, "markers": ["has spaces"]},
        {**GOOD, "sources": [{"channel_id": "lowercase", "ts": "1760781660.000100"}]},
        {**GOOD, "sources": [{"channel_id": "C0IDEAS01", "ts": "1"}]},
    ],
)
async def test_anything_off_shape_is_refused(issue: dict[str, Any]) -> None:
    runner, _ = client(200, {"issues": [issue]})
    with pytest.raises(RunnerError, match="not a list of marked issues"):
        await runner.marked_issues("acme-co", "web")


async def test_the_title_is_cleaned_again_here() -> None:
    runner, _ = client(200, {"issues": [{**GOOD, "title": "  Progress   bar​  "}]})
    found = await runner.marked_issues("acme-co", "web")
    assert found.issues[0].title == "Progress bar"


async def test_a_reply_too_large_and_a_bad_name_are_refused() -> None:
    runner, _ = client(200, b"x" * 8_000_001)
    with pytest.raises(RunnerError, match="too large"):
        await runner.marked_issues("acme-co", "web")
    with pytest.raises(RunnerError, match="not a GitHub repo"):
        await runner.marked_issues("acme-co", "..")
    failing, _ = client(502, {"error": "GitHub could not list the issues"})
    with pytest.raises(RunnerError, match="GitHub didn't answer"):
        await failing.marked_issues("acme-co", "web")


async def test_the_write_login_comes_from_the_execution_runner() -> None:
    runner, seen = client(200, {"login": "jotflow-bot"})
    assert await runner.write_login() == "jotflow-bot"
    assert seen[0].headers["host"] == "agent-exec:8766" and seen[0].url.path == "/identity"
    bad, _ = client(200, {"login": "jotflow-bot", "token": "x"})
    with pytest.raises(RunnerError):
        await bad.write_login()
