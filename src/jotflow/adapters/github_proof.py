"""Check a one-time code on a GitHub account, anonymously (#195).

The drafting runner does this, not JotFlow, so JotFlow's own container never
talks to GitHub. It needs no token: a user's public profile and public gists
are readable by anyone. It reads exactly two things:

- ``GET /users/{login}``: the account must be a User, not an organisation, and
  its ``bio`` may hold the code
- ``GET /users/{login}/gists?per_page=10``: the code may be a recent public
  gist's description

JotFlow keeps only the code's SHA-256 digest, so that's what it sends. The
runner hashes each code-shaped word it finds and compares, so the code itself
never travels or rests anywhere but in the person's own Slack reply and on
GitHub. Nothing else from the text is used, returned, or logged. A
rate-limited answer is reported as such, never as "not verified", so a person
is told to try again later.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jotflow.proof_codes import CODE, CODE_WORD, DIGEST, code_digest
from jotflow.store.base import check_github_login

API = "https://api.github.com"
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 10.0
GISTS_CHECKED = 10

__all__ = ["CODE", "DIGEST", "Proof", "ProofError", "check_proof", "code_digest", "contains_code"]
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "jotflow-identity-check",
}


class ProofError(Exception):
    """GitHub didn't give a usable answer. The message is fixed text."""


class Proof(BaseModel):
    """The answer, and the only thing that leaves the runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verified: bool
    rate_limited: bool = False
    github_id: int | None = Field(default=None, ge=1)
    login: str | None = None


class _User(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=1)
    login: str
    type: str
    bio: str | None = None


class _Gist(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str | None = None


def contains_code(text: str | None, digest: str) -> bool:
    """Whether a whole-word code in ``text`` hashes to ``digest``."""
    if not text:
        return False
    return any(hmac.compare_digest(code_digest(found), digest) for found in CODE_WORD.findall(text))


def _rate_limited(response: httpx2.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"


async def _get(http: httpx2.AsyncClient, path: str) -> tuple[int, Any, bool]:
    """(status, parsed JSON or None, rate limited), reading at most MAX_RESPONSE_BYTES."""
    try:
        async with http.stream("GET", API + path, headers=_HEADERS) as response:
            if _rate_limited(response):
                return response.status_code, None, True
            if response.status_code != 200:
                return response.status_code, None, False
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProofError("GitHub's answer was too large")
    except httpx2.HTTPError:
        raise ProofError("GitHub could not be reached") from None
    try:
        return 200, json.loads(body), False
    except ValueError:
        raise ProofError("GitHub's answer was not JSON") from None


def new_client(transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """No token, no redirects, no proxy settings from the environment."""
    return httpx2.AsyncClient(
        transport=transport,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    )


async def check_proof(http: httpx2.AsyncClient, login: str, digest: str) -> Proof:
    """Whether ``login``'s public profile or recent gists carry the code ``digest`` names."""
    check_github_login(login)
    if not DIGEST.fullmatch(digest):
        raise ValueError("not a code digest")
    status, data, limited = await _get(http, f"/users/{login}")
    if limited:
        return Proof(verified=False, rate_limited=True)
    if status == 404:
        return Proof(verified=False)
    if status != 200:
        raise ProofError(f"GitHub answered {status}")
    try:
        user = _User.model_validate(data)
    except ValidationError:
        raise ProofError("GitHub's answer was not a user") from None
    if user.type != "User":
        return Proof(verified=False)  # an organisation can't be a person's link
    found = contains_code(user.bio, digest)
    if not found:
        status, data, limited = await _get(http, f"/users/{login}/gists?per_page={GISTS_CHECKED}")
        if limited:
            return Proof(verified=False, rate_limited=True)
        if status == 200 and isinstance(data, list):
            for item in data[:GISTS_CHECKED]:
                try:
                    gist = _Gist.model_validate(item)
                except ValidationError:
                    continue
                if contains_code(gist.description, digest):
                    found = True
                    break
    if not found:
        return Proof(verified=False)
    return Proof(verified=True, github_id=user.id, login=user.login)
