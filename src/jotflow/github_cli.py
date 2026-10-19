"""Ask the installer's own GitHub CLI about a repo, during setup only (#122).

JotFlow never holds GitHub credentials (D5). ``jotflow init`` may still ask
``gh``, which uses the person's own login on their own machine, for a repo's
visibility, so the policy file can say whether a card should warn. Nothing it
returns is stored except "public" or "private", and nothing is printed: a
missing ``gh``, no login, no access, or a slow answer all mean "unknown", and
setup asks instead.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from jotflow.models import RepoVisibility

GH_TIMEOUT_SECONDS = 10.0

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
