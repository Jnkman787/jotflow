"""Asking the installer's gh for a repo's visibility, during setup only (#122)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from jotflow.github_cli import GH_TIMEOUT_SECONDS, gh_visibility


class Done:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def runner(result: Any) -> Any:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        if isinstance(result, BaseException):
            raise result
        return result

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("public\n", "public"), ("private\n", "private"), ("internal\n", "private"), ("x", None)],
)
def test_gh_answers_public_or_private(stdout: str, expected: str | None) -> None:
    run = runner(Done(stdout))
    assert gh_visibility("acme/web", which=lambda _: "/usr/bin/gh", run=run) == expected
    ((argv, kwargs),) = run.calls
    assert argv == ["/usr/bin/gh", "api", "repos/acme/web", "--jq", ".visibility"]
    assert kwargs["timeout"] == GH_TIMEOUT_SECONDS and kwargs["capture_output"] is True


@pytest.mark.parametrize(
    "outcome",
    [
        Done("", returncode=1),
        subprocess.TimeoutExpired("gh", GH_TIMEOUT_SECONDS),
        FileNotFoundError("gh"),
    ],
)
def test_anything_wrong_means_unknown(outcome: Any) -> None:
    assert gh_visibility("acme/web", which=lambda _: "/usr/bin/gh", run=runner(outcome)) is None


def test_without_gh_nothing_runs() -> None:
    run = runner(Done("public"))
    assert gh_visibility("acme/web", which=lambda _: None, run=run) is None
    assert run.calls == []


# Listing repos to pick from (#91)


def gh_found(name: str) -> str | None:
    return "/usr/bin/gh"


LISTING = (
    '[{"nameWithOwner": "acme-co/web", "description": "The  web\\napp", "visibility": "PRIVATE",'
    ' "isFork": false},'
    ' {"nameWithOwner": "acme-co/aaa-fork", "description": null, "visibility": "PUBLIC",'
    ' "isFork": true},'
    ' {"nameWithOwner": "acme-co/api", "description": "API", "visibility": "INTERNAL",'
    ' "isFork": false},'
    ' {"description": "no name"}]'
)


def test_repos_are_listed_read_only_sources_first() -> None:
    from jotflow.github_cli import ListedRepo, gh_repos

    run = runner(Done(LISTING))
    listed = gh_repos("acme-co", which=gh_found, run=run)
    assert listed == [
        ListedRepo("acme-co/api", "API", "private"),
        ListedRepo("acme-co/web", "The web app", "private"),
        ListedRepo("acme-co/aaa-fork", "", "public", fork=True),
    ]
    ((argv, kwargs),) = run.calls
    assert argv[:4] == ["/usr/bin/gh", "repo", "list", "acme-co"]
    assert "--no-archived" in argv and "--json" in argv
    assert kwargs["timeout"] == GH_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "outcome",
    [
        Done("[]", 1),
        Done("not json"),
        Done('{"a": 1}'),
        subprocess.TimeoutExpired("gh", 1),
        OSError(),
    ],
)
def test_a_listing_that_fails_means_type_instead(outcome: Any) -> None:
    from jotflow.github_cli import gh_repos

    assert gh_repos("acme-co", which=gh_found, run=runner(outcome)) is None


@pytest.mark.parametrize("owner", ["", "acme co", "-acme", "acme/web", "a" * 40, "*"])
def test_an_owner_that_isnt_a_github_name_lists_nothing(owner: str) -> None:
    from jotflow.github_cli import gh_repos

    run = runner(Done(LISTING))
    assert gh_repos(owner, which=gh_found, run=run) is None
    assert run.calls == []


def test_the_signed_in_login() -> None:
    from jotflow.github_cli import gh_login

    assert gh_login(which=gh_found, run=runner(Done('{"login": "acme-alice"}'))) == "acme-alice"
    assert gh_login(which=gh_found, run=runner(Done('{"login": "bad name"}'))) is None
    assert gh_login(which=lambda name: None, run=runner(Done("{}"))) is None
    assert gh_login(which=gh_found, run=runner(Done("", 4))) is None
