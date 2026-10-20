"""JotFlow's side of POST /verify (#195): only the digest goes, the answer is strict."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from jotflow.orchestrator.launch import RunnerClient, RunnerEndpoint, RunnerError, VerifyAnswer

SECRET = "s" * 40
DIGEST = "a" * 64


def client(status: int, body: Any) -> tuple[RunnerClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body)

    return (
        RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", SECRET),
            execution=RunnerEndpoint("http://agent-exec:8766", "e" * 40),
            transport=httpx2.MockTransport(handle),
        ),
        seen,
    )


async def test_only_the_login_and_digest_go_to_the_drafting_runner() -> None:
    runner, seen = client(
        200, {"verified": True, "rate_limited": False, "github_id": 4, "login": "Octo"}
    )
    answer = await runner.verify("octo", DIGEST)
    assert answer == VerifyAnswer(verified=True, rate_limited=False, github_id=4, login="Octo")
    [request] = seen
    assert request.headers["host"] == "agent:8766" and request.url.path == "/verify"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(request.content) == {"login": "octo", "code_digest": DIGEST}


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (502, {"error": "GitHub answered 500"}, "GitHub didn't answer"),
        (401, {"error": "x"}, "refused JotFlow's secret"),
        (200, {"verified": True, "rate_limited": False}, "not a verification answer"),
        (
            200,
            {"verified": True, "rate_limited": False, "github_id": 1, "login": "x", "bio": "y"},
            "not a verification answer",
        ),
        (200, {"verified": "yes", "rate_limited": False}, "not a verification answer"),
    ],
)
async def test_anything_else_is_an_error_not_a_verdict(status: int, body: Any, reason: str) -> None:
    runner, _ = client(status, body)
    with pytest.raises(RunnerError, match=reason):
        await runner.verify("octo", DIGEST)
