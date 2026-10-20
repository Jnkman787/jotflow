"""Check a one-time code on a GitHub account, anonymously (#195).

The drafting runner does this, not JotFlow, so JotFlow's own container never
talks to GitHub. It needs no token: a user's public profile and public gists
are readable by anyone. It reads exactly two things:

- ``GET /users/{login}``: the account must be a User, not an organisation, and
  its ``bio`` may hold the code
- ``GET /users/{login}/gists?per_page=10``: the code may be a recent public
  gist's description

The code must appear as a whole word. Nothing else from the text is used,
returned, or logged. A rate-limited answer is reported as such, never as "not
verified", so a person is told to try again later.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jotflow.store.base import check_github_login

API = "https://api.github.com"
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 10.0
GISTS_CHECKED = 10
CODE = re.compile(r"jotflow-[A-Za-z0-9_-]{12}")
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


def contains_code(text: str | None, code: str) -> bool:
    """Whether ``code`` appears in ``text`` as a whole word."""
    if not text:
        return False
    return re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", text) is not None


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


async def check_proof(http: httpx2.AsyncClient, login: str, code: str) -> Proof:
    """Whether ``login``'s public profile or recent gists carry ``code``."""
    check_github_login(login)
    if not CODE.fullmatch(code):
        raise ValueError("not a JotFlow verification code")
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
    found = contains_code(user.bio, code)
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
                if contains_code(gist.description, code):
                    found = True
                    break
    if not found:
        return Proof(verified=False)
    return Proof(verified=True, github_id=user.id, login=user.login)
