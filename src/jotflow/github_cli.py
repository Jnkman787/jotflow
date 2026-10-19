"""Ask the installer's own GitHub CLI about a repo, during setup only (#122).

JotFlow never holds GitHub credentials (D5). ``jotflow init`` may still ask
``gh``, which uses the person's own login on their own machine, for a repo's
visibility, so the policy file can say whether a card should warn. Nothing it
returns is stored except "public" or "private", and nothing is printed: a
missing ``gh``, no login, no access, or a slow answer all mean "unknown", and
setup asks instead.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jotflow.models import RepoVisibility

GH_TIMEOUT_SECONDS = 10.0
# A GitHub user or organisation name.
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

Which = Callable[[str], str | None]
Run = Callable[..., Any]


def gh_visibility(
    repo: str, *, which: Which = shutil.which, run: Run = subprocess.run
) -> RepoVisibility | None:
    """``public`` or ``private`` for ``owner/name``, from ``gh``, or None if it can't say.

    GitHub's ``internal`` (an enterprise repo visible to its members) counts
    as private: it is not visible to anyone, which is what the warning is for.
    """
    gh = which("gh")
    if gh is None:
        return None
    try:
        done = run(
            [gh, "api", f"repos/{repo}", "--jq", ".visibility"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    answer = str(done.stdout).strip().lower()
    if answer == "public":
        return "public"
    if answer in ("private", "internal"):
        return "private"
    return None


# Picking repos from a list during setup (#91): two more read-only calls.

GH_LIST_LIMIT = 200


@dataclass(frozen=True)
class ListedRepo:
    """One repo from ``gh repo list``: what setup offers as defaults, nothing more."""

    repo: str
    description: str
    visibility: RepoVisibility | None
    fork: bool = False


def _gh_json(args: list[str], which: Which, run: Run) -> Any:
    gh = which("gh")
    if gh is None:
        return None
    try:
        done = run(
            [gh, *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None  # stderr is never shown: it can name the account
    try:
        return json.loads(str(done.stdout))
    except ValueError:
        return None


def gh_login(*, which: Which = shutil.which, run: Run = subprocess.run) -> str | None:
    """The login ``gh`` is signed in as, or None without a usable ``gh``."""
    answer = _gh_json(["api", "user"], which, run)
    login = answer.get("login") if isinstance(answer, dict) else None
    return login if isinstance(login, str) and _OWNER.fullmatch(login) else None


def gh_repos(
    owner: str, *, which: Which = shutil.which, run: Run = subprocess.run
) -> list[ListedRepo] | None:
    """``owner``'s repos that are not archived, sources before forks, or None if ``gh`` can't say.

    Only reads. Nothing here is stored but the repos a person then picks.
    """
    if not _OWNER.fullmatch(owner):
        return None
    fields = "nameWithOwner,description,visibility,isFork"
    answer = _gh_json(
        ["repo", "list", owner, "--no-archived", "--limit", str(GH_LIST_LIMIT), "--json", fields],
        which,
        run,
    )
    if not isinstance(answer, list):
        return None
    listed: list[ListedRepo] = []
    for item in answer:
        if not isinstance(item, dict) or not isinstance(item.get("nameWithOwner"), str):
            continue
        description = item.get("description")
        visibility = str(item.get("visibility") or "").lower()
        listed.append(
            ListedRepo(
                repo=item["nameWithOwner"],
                description=" ".join(str(description).split()) if description else "",
                visibility="public"
                if visibility == "public"
                else "private"
                if visibility in ("private", "internal")
                else None,
                fork=bool(item.get("isFork")),
            )
        )
    listed.sort(key=lambda r: (r.fork, r.repo.casefold()))
    return listed
