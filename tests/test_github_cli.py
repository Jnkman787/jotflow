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
