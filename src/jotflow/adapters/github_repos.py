"""The runner's own GitHub calls for pattern scopes (#236). Never the agent's.

Two fixed requests, each built here from a validated owner and name:

- :func:`list_repos`, on the drafting runner with the read token: an owner's
  repos, for JotFlow's catalog
- :func:`reachable`, on the execution runner with the write token: whether that
  token can see one repo, checked before an approval is accepted

Only the token and the path come from outside this module, and neither leaves
it. GitHub's answer is validated here too, not only in JotFlow. A strict model
keeps eight fields, a repo under another owner is dropped and counted, the size
caps hold while reading, redirects are never followed, and the next page is
built from a page number, never taken from GitHub's ``Link`` header.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import ValidationError

from jotflow.models import DiscoveredRepo, is_bare_repo_name, is_owner_name

API = "https://api.github.com"
PAGE_SIZE = 100
MAX_REPOS = 1000
MAX_BYTES = 1_000_000
# More than enough pages for MAX_REPOS of one owner, in case an answer misbehaves.
MAX_PAGES = 20
TIMEOUT_SECONDS = 15.0


class GithubError(Exception):
    """GitHub answered with something other than what was asked for."""


@dataclass
class Listing:
    """An owner's repos, and what was left out on the way."""

    repos: list[DiscoveredRepo] = field(default_factory=list)
    dropped: int = 0  # under another owner, or not a valid repo
    truncated: bool = False  # a cap was reached

    def as_json(self) -> dict[str, Any]:
        return {
            "repos": [r.model_dump(mode="json") for r in self.repos],
            "dropped": self.dropped,
            "truncated": self.truncated,
        }


def client(*, transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """An HTTP client for these calls: no redirects, no proxy settings from the environment."""
    return httpx2.AsyncClient(
        transport=transport,
        timeout=httpx2.Timeout(TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jotflow",
    }


def _rate_limited(response: httpx2.Response) -> bool:
    """GitHub's rate limits: a 429, or a 403 with no requests left or a Retry-After."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    headers = response.headers
    return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers


async def _page(http: httpx2.AsyncClient, url: str, token: str, budget: int) -> tuple[int, bytes]:
    """One page's status and body, read up to ``budget`` bytes.

    A rate limit raises: it says nothing about the token's reach, so a caller
    must never read it as "can't reach" (#236).
    """
    async with http.stream("GET", url, headers=_headers(token)) as response:
        if _rate_limited(response):
            raise GithubError("GitHub's rate limit")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > budget:
                break
        return response.status_code, body


async def list_repos(
    owner: str, token: str, *, http: httpx2.AsyncClient, api: str = API
) -> Listing:
    """Every repo ``token`` can see under ``owner``, an org or the token's own user."""
    if not is_owner_name(owner):
        raise GithubError("not a GitHub owner")
    listing = Listing()
    used = 0
    bases = (
        f"{api}/orgs/{owner}/repos?type=all&per_page={PAGE_SIZE}",
        f"{api}/user/repos?affiliation=owner,collaborator,organization_member&per_page={PAGE_SIZE}",
    )
    prefix = f"{owner.casefold()}/"
    for attempt, base in enumerate(bases):
        page = 1
        while True:
            status, body = await _page(http, f"{base}&page={page}", token, MAX_BYTES - used)
            if status == 404 and page == 1 and attempt == 0:
                break  # not an org: try the token's own repos, filtered to the owner
            if status != 200:
                raise GithubError(f"GitHub answered {status}")
            used += len(body)
            if used > MAX_BYTES:
                listing.truncated = True
                return listing
            try:
                items = json.loads(body)
            except ValueError:
                raise GithubError("GitHub's answer was not JSON") from None
            if not isinstance(items, list):
                raise GithubError("GitHub's answer was not a list")
            for item in items:
                try:
                    repo = DiscoveredRepo.model_validate(item)
                except ValidationError:
                    listing.dropped += 1
                    continue
                name = repo.full_name.partition("/")[2]
                if not repo.full_name.casefold().startswith(prefix) or not is_bare_repo_name(name):
                    listing.dropped += 1
                    continue
                if len(listing.repos) >= MAX_REPOS:
                    listing.truncated = True
                    return listing
                listing.repos.append(repo)
            if len(items) < PAGE_SIZE:
                return listing
            page += 1
            if page > MAX_PAGES:
                listing.truncated = True
                return listing
    return listing


async def reachable(
    owner: str, name: str, token: str, *, http: httpx2.AsyncClient, api: str = API
) -> bool:
    """Whether ``token`` can push to ``owner/name``, by GitHub's ``permissions.push``.

    Seeing the repo isn't enough: any token sees a public repo. A fine-grained
    token outside its repo list gets 404. Anything but ``push: true`` is a no.
    """
    if not is_owner_name(owner) or not is_bare_repo_name(name):
        raise GithubError("not a GitHub repo")
    status, body = await _page(http, f"{api}/repos/{owner}/{name}", token, 64 * 1024)
    if status in (403, 404):
        return False
    if status != 200:
        raise GithubError(f"GitHub answered {status}")
    try:
        answer = json.loads(body)
    except ValueError:
        raise GithubError("GitHub's answer was not JSON") from None
    permissions = answer.get("permissions") if isinstance(answer, dict) else None
    return isinstance(permissions, dict) and permissions.get("push") is True
