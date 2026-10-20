"""The repo catalog: listed repos, scopes found live, and the write allowlist (#236)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jotflow.models import DiscoveredRepo
from jotflow.policy import Policy, RepoNotAllowedError
from jotflow.repo_catalog import (
    ACCESS_FOR,
    RENAME_GRACE,
    STALE_AFTER,
    RepoCatalog,
    RepoRefused,
    default_nickname,
)

CH = "C0TEST1"


def gh(n: int, name: str, *, owner: str = "example-org", **fields: Any) -> DiscoveredRepo:
    base: dict[str, Any] = {
        "id": n,
        "full_name": f"{owner}/{name}",
        "description": f"The {name} repo",
        "topics": ["web"],
        "pushed_at": f"2026-10-{10 + n:02d}T00:00:00Z",
        "private": True,
    }
    base.update(fields)
    return DiscoveredRepo.model_validate(base)


def policy(**scope: Any) -> Policy:
    return Policy.model_validate(
        {
            "channels": {
                CH: {
                    "repos": [
                        {"repo": "example-org/handbook", "nickname": "docs", "description": "Docs"}
                    ],
                    "scopes": [
                        {
                            "owner": "example-org",
                            "match": ["web-*", "api", "handbook"],
                            "exclude": ["*-demo"],
                            **scope,
                        }
                    ],
                }
            }
        }
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.now


class Fake:
    """The drafting runner's listing and the execution runner's access check."""

    def __init__(self, repos: list[DiscoveredRepo]) -> None:
        self.repos = repos
        self.listings = 0
        self.fail = False
        self.reachable: dict[str, bool] = {}
        self.asked: list[str] = []

    async def lister(self, owner: str) -> list[DiscoveredRepo]:
        self.listings += 1
        if self.fail:
            raise ConnectionError("down")
        return list(self.repos)

    async def access(self, owner: str, name: str) -> bool:
        self.asked.append(f"{owner}/{name}")
        return self.reachable.get(f"{owner}/{name}", False)


REPOS = [
    gh(1, "web-frontend"),
    gh(2, "web-backend", private=False),
    gh(3, "api"),
    gh(4, "web-demo"),  # excluded by glob
    gh(5, "web-old", archived=True),
    gh(6, "web-fork", fork=True),
    gh(7, "billing"),  # not matched
    gh(8, "handbook"),  # listed by hand, which wins
]


def catalog(fake: Fake, clock: Clock, **scope: Any) -> RepoCatalog:
    return RepoCatalog(
        policy(**scope),
        lister=fake.lister,
        access=fake.access,
        clock=clock,
        mono=lambda: clock.mono,
    )


def test_without_scopes_the_catalog_is_the_policys_own_list() -> None:
    static = Policy.model_validate(
        {"channels": {CH: {"repos": [{"repo": "a/b", "nickname": "b", "description": "B"}]}}}
    )
    repos = RepoCatalog.static(static)
    assert repos.repos(CH) == static.channels[CH].repos
    assert repos.repo(CH, "b") is static.channels[CH].repos[0]
    with pytest.raises(RepoNotAllowedError):
        repos.repo(CH, "a/c")


async def test_a_scope_lists_matching_repos_newest_first_and_leaves_out_the_rest() -> None:
    fake, clock = Fake(REPOS), Clock()
    repos = catalog(fake, clock, labels=["bug"], description="Our product")
    assert [r.repo for r in repos.repos(CH)] == ["example-org/handbook"]  # nothing listed yet
    await repos.refresh("example-org")
    names = [r.repo for r in repos.repos(CH)]
    assert names == [
        "example-org/handbook",  # by hand, first
        "example-org/api",
        "example-org/web-backend",
        "example-org/web-frontend",
    ]
    api = repos.repo(CH, "example-org/api")
    assert api.labels == ("bug",) and api.description == "Our product"
    assert api.visibility == "private"
    assert repos.repo(CH, "example-org/web-backend").visibility == "public"
    assert repos.scope_total(CH) == 3
    assert repos.repo(CH, "docs").repo == "example-org/handbook"


async def test_each_refusal_says_why() -> None:
    fake, clock = Fake(REPOS), Clock()
    repos = catalog(fake, clock)
    await repos.refresh("example-org")
    with pytest.raises(RepoRefused, match="is archived"):
        repos.repo(CH, "example-org/web-old")
    with pytest.raises(RepoRefused, match="is a fork"):
        repos.repo(CH, "example-org/web-fork")
    for outside in ("example-org/web-demo", "example-org/billing", "other-org/web-x", "web-x"):
        with pytest.raises(RepoNotAllowedError):
            repos.repo(CH, outside)


async def test_a_new_repo_is_picked_up_on_a_miss_without_a_restart() -> None:
    fake, clock = Fake(REPOS), Clock()
    repos = catalog(fake, clock)
    await repos.refresh("example-org")
    fake.repos = [*REPOS, gh(9, "web-new")]
    with pytest.raises(RepoRefused, match="refreshing"):
        repos.repo(CH, "example-org/web-new")
    await asyncio.sleep(0)  # let the triggered refresh run
    await asyncio.sleep(0)
    assert repos.repo(CH, "example-org/web-new").nickname == "web-new"
    # A second miss within the minute asks no more.
    before = fake.listings
    with pytest.raises(RepoRefused):
        repos.repo(CH, "example-org/web-later")
    await asyncio.sleep(0)
    assert fake.listings == before


async def test_a_renamed_repo_resolves_by_its_new_name_and_its_old_one_for_a_while() -> None:
    fake, clock = Fake([gh(1, "web-frontend")]), Clock()
    repos = catalog(fake, clock)
    await repos.refresh("example-org")
    fake.repos = [gh(1, "web-site")]
    await repos.refresh("example-org")
    assert repos.repo(CH, "example-org/web-site").repo == "example-org/web-site"
    assert repos.repo(CH, "example-org/web-frontend").repo == "example-org/web-site"
    clock.now += RENAME_GRACE + timedelta(minutes=1)
    await repos.refresh("example-org")
    with pytest.raises(RepoRefused):
        repos.repo(CH, "example-org/web-frontend")


async def test_a_stale_listing_fails_closed_and_a_failed_refresh_keeps_the_last() -> None:
    fake, clock = Fake(REPOS), Clock()
    repos = catalog(fake, clock)
    await repos.refresh("example-org")
    fake.fail = True
    assert not await repos.refresh("example-org")
    assert repos.repo(CH, "example-org/api")  # the last listing still serves
    clock.now += STALE_AFTER + timedelta(minutes=1)
    with pytest.raises(RepoRefused, match="refreshing"):
        repos.repo(CH, "example-org/api")
    assert repos.repo(CH, "docs")  # listed repos never depend on discovery


async def test_nicknames_by_precedence_and_collision() -> None:
    fake = Fake([gh(1, "Web.Frontend"), gh(2, "docs"), gh(3, "web-site")])
    clock = Clock()
    repos = catalog(fake, clock, match=["*"], aliases={"site": "web-site"})
    await repos.refresh("example-org")
    by_repo = {r.repo: r.nickname for r in repos.repos(CH)}
    assert by_repo["example-org/Web.Frontend"] == "web-frontend"
    assert by_repo["example-org/web-site"] == "site"  # the alias beats the default
    # "docs" is taken by the listed handbook repo, so the found one falls back.
    assert by_repo["example-org/docs"] == "example-org-docs"
    assert repos.repo(CH, "site").repo == "example-org/web-site"
    assert default_nickname("...") is None and default_nickname("API.v2") == "api-v2"


async def test_github_text_is_cleaned_and_kept_apart() -> None:
    noisy = gh(
        1,
        "web-x",
        description="Ping <!here> and see https://evil.example/x @someone " + "y" * 300,
        topics=["web", "Bad Topic", "ok-2", *[f"t{i}" for i in range(20)]],
    )
    fake, clock = Fake([noisy]), Clock()
    repos = catalog(fake, clock)
    await repos.refresh("example-org")
    details = repos.details(CH, "example-org/web-x")
    assert details.discovered and details.description is not None
    assert "here" not in details.description and "evil.example" not in details.description
    assert "@someone" not in details.description and len(details.description) <= 200
    assert details.topics[:2] == ("web", "ok-2") and len(details.topics) == 10
    # The policy text an agent chooses by stays the scope's, never GitHub's.
    assert repos.repo(CH, "example-org/web-x").description == ""
    assert not repos.details(CH, "example-org/handbook").discovered


async def test_write_access_is_checked_for_found_repos_and_only_a_yes_is_kept() -> None:
    fake, clock = Fake(REPOS), Clock()
    repos = catalog(fake, clock)
    await repos.refresh("example-org")
    assert await repos.writable(CH, "example-org/handbook")  # listed: not asked
    assert fake.asked == []
    assert not await repos.writable(CH, "example-org/api")
    assert not await repos.writable(CH, "example-org/api")
    assert fake.asked == ["example-org/api", "example-org/api"]  # a no is never kept
    fake.reachable["example-org/api"] = True
    assert await repos.writable(CH, "example-org/api")
    assert await repos.writable(CH, "example-org/api")
    assert fake.asked.count("example-org/api") == 3  # a yes is kept
    clock.now += ACCESS_FOR + timedelta(seconds=1)
    assert await repos.writable(CH, "example-org/api")
    assert fake.asked.count("example-org/api") == 4


async def test_a_failed_access_check_raises_so_the_caller_fails_closed() -> None:
    async def down(owner: str, name: str) -> bool:
        raise ConnectionError("down")

    clock = Clock()
    fake = Fake(REPOS)
    repos = RepoCatalog(policy(), lister=fake.lister, access=down, clock=clock)
    await repos.refresh("example-org")
    with pytest.raises(ConnectionError):
        await repos.writable(CH, "example-org/api")


async def test_each_scope_has_a_summary_line() -> None:
    fake, clock = Fake(REPOS), Clock()
    repos = catalog(fake, clock)
    [line] = repos.summaries(CH)
    assert "not listed yet" in line
    await repos.refresh("example-org")
    clock.now += timedelta(minutes=5)
    [line] = repos.summaries(CH)
    assert line.startswith("Also every repo in example-org matching web-*, api, handbook")
    assert "except *-demo, forks, archived repos" in line and "5 min ago" in line
