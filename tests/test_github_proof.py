"""Checking a one-time code on a GitHub account, in the drafting runner (#195)."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from jotflow.adapters import github_proof
from jotflow.adapters.github_proof import (
    Proof,
    ProofError,
    check_proof,
    code_digest,
    contains_code,
)

CODE = "jotflow-Ab3_xY9-kLm2"
OTHER = "jotflow-ZZZZZZZZZZZZ"
DIGEST = code_digest(CODE)


class FakeGitHub:
    """Answers the two allowed paths from a table, and records every request."""

    def __init__(
        self,
        user: dict[str, Any] | None = None,
        gists: list[dict[str, Any]] | None = None,
        *,
        status: int = 200,
        gist_status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        self.user = user if user is not None else {"id": 42, "login": "Octo-Cat", "type": "User"}
        self.gists = gists or []
        self.status = status
        self.gist_status = gist_status
        self.headers = headers or {}
        self.body = body
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path.endswith("/gists"):
            return httpx2.Response(self.gist_status, json=self.gists, headers=self.headers)
        if self.body is not None:
            return httpx2.Response(self.status, content=self.body, headers=self.headers)
        return httpx2.Response(self.status, json=self.user, headers=self.headers)


async def check(fake: FakeGitHub, login: str = "octo-cat", digest: str = DIGEST) -> Proof:
    async with github_proof.new_client(httpx2.MockTransport(fake)) as http:
        return await check_proof(http, login, digest)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        (f"Hi! {CODE}", True),
        (f"({CODE})", True),
        (f"{CODE}x", False),
        (f"x-{CODE}", False),
        (f"{CODE}-more", False),
        (None, False),
        ("", False),
    ],
)
def test_the_code_must_be_a_whole_word(text: str | None, found: bool) -> None:
    assert contains_code(text, DIGEST) is found


async def test_a_code_in_the_bio_verifies_with_the_canonical_login_and_id() -> None:
    fake = FakeGitHub({"id": 42, "login": "Octo-Cat", "type": "User", "bio": f"me {CODE}"})
    proof = await check(fake)
    assert proof == Proof(verified=True, github_id=42, login="Octo-Cat")
    [request] = fake.requests
    assert str(request.url) == "https://api.github.com/users/octo-cat"
    assert "authorization" not in request.headers


async def test_a_code_in_a_recent_gist_description_verifies() -> None:
    gists = [{"description": "notes"}, {"description": f"proof {CODE}"}]
    fake = FakeGitHub(gists=gists)
    assert (await check(fake)).verified
    assert [str(r.url) for r in fake.requests] == [
        "https://api.github.com/users/octo-cat",
        "https://api.github.com/users/octo-cat/gists?per_page=10",
    ]


async def test_only_the_profile_and_gists_are_ever_read() -> None:
    fake = FakeGitHub({"id": 1, "login": "octo-cat", "type": "User", "bio": OTHER})
    await check(fake)
    for request in fake.requests:
        assert request.url.host == "api.github.com"
        assert request.url.path in ("/users/octo-cat", "/users/octo-cat/gists")
        assert request.method == "GET"
        assert "authorization" not in request.headers


@pytest.mark.parametrize(
    "fake",
    [
        FakeGitHub({"id": 1, "login": "octo-cat", "type": "User", "bio": OTHER}),
        FakeGitHub({"id": 1, "login": "acme", "type": "Organization", "bio": CODE}),
        FakeGitHub(status=404),
        FakeGitHub(gists=[{"description": f"x{CODE}"}, {"oops": 1}, {"description": None}]),
    ],
)
async def test_no_code_an_organisation_or_no_account_is_not_verified(fake: FakeGitHub) -> None:
    assert await check(fake) == Proof(verified=False)


@pytest.mark.parametrize(
    ("status", "headers", "gists_limited"),
    [
        (429, {}, False),
        (403, {"x-ratelimit-remaining": "0"}, False),
        (200, {}, True),
    ],
)
async def test_a_rate_limit_is_never_not_verified(
    status: int, headers: dict[str, str], gists_limited: bool
) -> None:
    fake = FakeGitHub(status=status, headers={} if gists_limited else headers)
    if gists_limited:
        fake.gist_status, fake.headers = 429, {}
    proof = await check(fake)
    assert proof == Proof(verified=False, rate_limited=True)


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (FakeGitHub(status=500), "GitHub answered 500"),
        (FakeGitHub(status=403), "GitHub answered 403"),
        (FakeGitHub(body=b"not json"), "not JSON"),
        (FakeGitHub(body=json.dumps({"login": "x"}).encode()), "not a user"),
        (FakeGitHub(body=b"[" + b"1," * 40_000 + b"1]"), "too large"),
    ],
)
async def test_an_unusable_answer_is_an_error_not_a_verdict(fake: FakeGitHub, reason: str) -> None:
    with pytest.raises(ProofError, match=reason):
        await check(fake)


async def test_github_unreachable_is_an_error() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    async with github_proof.new_client(httpx2.MockTransport(down)) as http:
        with pytest.raises(ProofError, match="could not be reached"):
            await check_proof(http, "octo-cat", DIGEST)


@pytest.mark.parametrize(
    ("login", "digest"),
    [("not a login", DIGEST), ("-bad", DIGEST), ("octo-cat", CODE), ("octo-cat", "A" * 64)],
)
async def test_a_bad_login_or_digest_is_refused_before_any_request(login: str, digest: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(ValueError):
        await check(fake, login, digest)
    assert fake.requests == []


def test_the_client_sends_no_token_and_follows_no_redirect() -> None:
    client = github_proof.new_client()
    assert client.follow_redirects is False
    assert "authorization" not in client.headers


def test_another_code_shaped_word_never_matches_the_digest() -> None:
    assert not contains_code(f"{OTHER} and jotflow-Ab3_xY9-kLm3", DIGEST)
    assert contains_code(f"{OTHER} then {CODE}", DIGEST)
