"""The runner's own GitHub calls for pattern scopes (#236), against a fake GitHub."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters import github_repos
from jotflow.adapters.github_repos import GithubError, list_repos, reachable
from jotflow.adapters.runner import Runner, build_runner_app
from tests.test_runner import HOST, READ, SECRET, WRITE, config


def repo(n: int, owner: str = "example-org", **extra: Any) -> dict[str, Any]:
    """A repo as GitHub's API returns it, with fields discovery must ignore."""
    return {
        "id": n,
        "full_name": f"{owner}/repo-{n}",
        "description": f"Repo {n}",
        "topics": ["web"],
        "pushed_at": "2026-10-19T12:00:00Z",
        "archived": False,
        "fork": False,
        "private": True,
        "owner": {"login": owner},
        "permissions": {"admin": True},
        "clone_url": "https://github.com/x.git",
        **extra,
    }


class FakeGithub:
    """Answers GitHub's REST paths from a table, and records every request."""

    def __init__(self, routes: dict[str, Callable[[httpx2.Request], httpx2.Response]]) -> None:
        self.routes = routes
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        handler = self.routes.get(request.url.path.lower())  # GitHub ignores case here
        if handler is None:
            return httpx2.Response(404, json={"message": "Not Found"})
        return handler(request)

    def client(self) -> httpx2.AsyncClient:
        return github_repos.client(transport=httpx2.MockTransport(self))


def pages(items: list[dict[str, Any]], *, link: bool = False) -> Callable[..., httpx2.Response]:
    def answer(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        chunk = items[(page - 1) * 100 : page * 100]
        # A Link header that points elsewhere, which discovery must never follow.
        headers = {"Link": '<https://evil.example/next>; rel="next"'} if link else {}
        return httpx2.Response(200, json=chunk, headers=headers)

    return answer


async def test_an_orgs_repos_are_listed_page_by_page_with_eight_fields() -> None:
    fake = FakeGithub(
        {"/orgs/example-org/repos": pages([repo(n) for n in range(1, 106)], link=True)}
    )
    listing = await list_repos("example-org", READ, http=fake.client())
    assert len(listing.repos) == 105 and listing.dropped == 0 and not listing.truncated
    assert set(listing.repos[0].model_dump()) == {
        "id",
        "full_name",
        "description",
        "topics",
        "pushed_at",
        "archived",
        "fork",
        "private",
    }
    # Every request went to GitHub's API with the token, never to the Link header's host.
    assert {r.url.host for r in fake.seen} == {"api.github.com"}
    assert [r.url.params["page"] for r in fake.seen] == ["1", "2"]
    assert all(r.headers["Authorization"] == f"Bearer {READ}" for r in fake.seen)


async def test_a_repo_under_another_owner_or_malformed_is_dropped_and_counted() -> None:
    items = [repo(1), repo(2, owner="other-org"), {"id": "x"}, repo(3, full_name="example-org/..")]
    fake = FakeGithub({"/orgs/example-org/repos": pages(items)})
    listing = await list_repos("Example-Org", READ, http=fake.client())
    assert [r.full_name for r in listing.repos] == ["example-org/repo-1"]
    assert listing.dropped == 3


async def test_a_user_owner_is_listed_from_the_tokens_own_repos() -> None:
    items = [repo(1, owner="sam"), repo(2, owner="example-org")]
    fake = FakeGithub({"/user/repos": pages(items)})
    listing = await list_repos("sam", READ, http=fake.client())
    assert [r.full_name for r in listing.repos] == ["sam/repo-1"] and listing.dropped == 1


async def test_the_repo_and_size_caps_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGithub({"/orgs/example-org/repos": pages([repo(n) for n in range(1, 250)])})
    monkeypatch.setattr(github_repos, "MAX_REPOS", 150)
    listing = await list_repos("example-org", READ, http=fake.client())
    assert len(listing.repos) == 150 and listing.truncated
    monkeypatch.setattr(github_repos, "MAX_REPOS", 1000)
    monkeypatch.setattr(github_repos, "MAX_BYTES", 2000)
    listing = await list_repos("example-org", READ, http=fake.client())
    assert listing.truncated and not listing.repos


async def test_a_redirect_or_an_error_is_never_followed() -> None:
    def moved(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "https://evil.example/repos"})

    fake = FakeGithub({"/orgs/example-org/repos": moved})
    with pytest.raises(GithubError, match="302"):
        await list_repos("example-org", READ, http=fake.client())
    assert {r.url.host for r in fake.seen} == {"api.github.com"}
    with pytest.raises(GithubError, match="not a GitHub owner"):
        await list_repos("example-*", READ, http=fake.client())


async def test_write_access_is_whether_the_token_can_see_the_repo() -> None:
    def one(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=repo(7))

    fake = FakeGithub({"/repos/example-org/web": one})
    assert await reachable("example-org", "web", WRITE, http=fake.client())
    assert not await reachable("example-org", "api", WRITE, http=fake.client())
    with pytest.raises(GithubError):
        await reachable("example-org", "..", WRITE, http=fake.client())


# The runner's endpoints


@asynccontextmanager
async def runner_http(
    tmp_path: Path, fake: FakeGithub, *, execution: bool = False
) -> AsyncIterator[httpx2.AsyncClient]:
    cfg = config(tmp_path, execution=execution)
    app = build_runner_app(Runner(cfg), cfg, github=fake.client())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=f"http://{HOST}",
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as http:
        yield http


async def test_the_drafting_runner_serves_discovery_and_not_access(tmp_path: Path) -> None:
    fake = FakeGithub({"/orgs/example-org/repos": pages([repo(1), repo(2)])})
    async with runner_http(tmp_path, fake) as http:
        answer = await http.get("/repos/example-org")
        assert answer.status_code == 200
        body = answer.json()
        assert [r["full_name"] for r in body["repos"]] == [
            "example-org/repo-1",
            "example-org/repo-2",
        ]
        assert READ not in answer.text
        assert (await http.get("/repos/example-org/web/access")).status_code == 404
        assert (await http.get("/repos/example*")).status_code == 400
        # The gate's rule stays: no query strings.
        assert (await http.get("/repos/example-org?type=all")).status_code == 400
        assert (
            await http.get("/repos/example-org", headers={"Authorization": "Bearer x"})
        ).status_code == 401


async def test_the_execution_runner_serves_access_and_not_discovery(tmp_path: Path) -> None:
    def one(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=repo(7))

    fake = FakeGithub({"/repos/example-org/web": one})
    async with runner_http(tmp_path, fake, execution=True) as http:
        assert (await http.get("/repos/example-org/web/access")).json() == {"reachable": True}
        assert (await http.get("/repos/example-org/api/access")).json() == {"reachable": False}
        assert (await http.get("/repos/example-org")).status_code == 404
        assert (await http.get("/repos/example-org/../access")).status_code in (400, 404)
    assert all(r.headers["Authorization"] == f"Bearer {WRITE}" for r in fake.seen)


async def test_a_github_failure_is_a_502_without_detail(tmp_path: Path) -> None:
    def broken(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="internal detail " + json.dumps({"token": "x"}))

    fake = FakeGithub({"/orgs/example-org/repos": broken})
    async with runner_http(tmp_path, fake) as http:
        answer = await http.get("/repos/example-org")
    assert answer.status_code == 502 and "internal detail" not in answer.text
