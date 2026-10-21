"""The runner's own GitHub reads for `jotflow rebuild --from-github` (#256). Never the agent's.

Two fixed requests, each built here, with the same client rules as
:mod:`jotflow.adapters.github_repos`: no redirects, no proxy settings from the
environment, a timeout, a byte cap while each page is read, and the next page
built from a page number, never taken from GitHub's ``Link`` header.

- :func:`marked_issues`, on the drafting runner with the read token: one repo's
  issues that carry a JotFlow marker. Each is parsed here into pointers and a
  cleaned title. The body, comments, labels and assignees never leave this
  module, so issue text never reaches JotFlow.
- :func:`token_login`, on the execution runner with the write token: that
  token's own login, from ``GET /user``, so JotFlow can trust only issues its
  own write token authored. Anyone can write a marker line into an issue.
"""

from __future__ import annotations

import json
import re
from datetime import date

import httpx2
from pydantic import ValidationError

from jotflow.adapters.github_repos import API, GithubError, _page
from jotflow.models import (
    MAX_ISSUE_SOURCES,
    MAX_MARKED_ISSUES,
    IssueDetails,
    IssueSource,
    MarkedIssue,
    MarkedIssues,
    is_bare_repo_name,
    is_owner_name,
    is_plain_label,
)
from jotflow.store.titles import clean_title

PAGE_SIZE = 100
MAX_PAGES = MAX_MARKED_ISSUES // PAGE_SIZE
MAX_BYTES = 30_000_000
MAX_MARKERS = 20

# Exactly the marker line and permalink execution writes (execution/writes.py).
_MARKER = re.compile(r"^<!-- jotflow:proposal:([A-Za-z0-9_-]{1,64}) -->$", re.MULTILINE)
_PERMALINK = re.compile(
    r"https://[a-z0-9-]+\.slack\.com/archives/(?P<channel>[CG][A-Z0-9]+)/p(?P<digits>[0-9]{16})"
    r"(?:\?thread_ts=(?P<thread>[0-9]{10}\.[0-9]{6})&cid=(?P=channel))?"
)


def _sources(body: str) -> tuple[IssueSource, ...]:
    found: list[IssueSource] = []
    for match in _PERMALINK.finditer(body):
        digits = match["digits"]
        try:
            source = IssueSource(
                channel_id=match["channel"],
                ts=f"{digits[:-6]}.{digits[-6:]}",
                thread_ts=match["thread"],
            )
        except ValidationError:
            continue
        if source not in found:
            found.append(source)
        if len(found) >= MAX_ISSUE_SOURCES:
            break
    return tuple(found)


def parse_issue(item: object) -> MarkedIssue | None:
    """One GitHub issue as JotFlow may see it, or None if it carries no marker."""
    if not isinstance(item, dict) or "pull_request" in item:
        return None
    body = item.get("body")
    user = item.get("user")
    if not isinstance(body, str) or not isinstance(user, dict):
        return None
    markers = tuple(dict.fromkeys(_MARKER.findall(body)))[:MAX_MARKERS]
    if not markers:
        return None
    raw_title = item.get("title")
    try:
        title = clean_title(raw_title if isinstance(raw_title, str) else "")
    except ValueError:
        title = ""
    try:
        return MarkedIssue(
            number=item.get("number"),  # type: ignore[arg-type]
            state=item.get("state"),  # type: ignore[arg-type]
            title=title or f"#{item.get('number')}",
            author=user.get("login"),  # type: ignore[arg-type]
            markers=markers,
            sources=_sources(body),
        )
    except ValidationError:
        return None


async def marked_issues(
    owner: str,
    name: str,
    token: str,
    *,
    since: date | None = None,
    http: httpx2.AsyncClient,
    api: str = API,
) -> MarkedIssues:
    """``owner/name``'s issues that carry a JotFlow marker, newest first, bounded."""
    if not is_owner_name(owner) or not is_bare_repo_name(name):
        raise GithubError("not a GitHub repo")
    base = f"{api}/repos/{owner}/{name}/issues?state=all&sort=created&direction=desc"
    base += f"&per_page={PAGE_SIZE}"
    if since is not None:
        base += f"&since={since.isoformat()}T00:00:00Z"
    issues: list[MarkedIssue] = []
    used = 0
    for page in range(1, MAX_PAGES + 1):
        status, body = await _page(http, f"{base}&page={page}", token, MAX_BYTES - used)
        if status != 200:
            raise GithubError(f"GitHub answered {status}")
        used += len(body)
        if used > MAX_BYTES:
            return MarkedIssues(issues=tuple(issues), truncated=True)
        try:
            items = json.loads(body)
        except ValueError:
            raise GithubError("GitHub's answer was not JSON") from None
        if not isinstance(items, list):
            raise GithubError("GitHub's answer was not a list")
        for item in items:
            issue = parse_issue(item)
            if issue is not None:
                issues.append(issue)
        if len(items) < PAGE_SIZE:
            return MarkedIssues(issues=tuple(issues))
    return MarkedIssues(issues=tuple(issues), truncated=True)


async def token_login(token: str, *, http: httpx2.AsyncClient, api: str = API) -> str:
    """The login ``token`` acts as, from ``GET /user``."""
    status, body = await _page(http, f"{api}/user", token, 64 * 1024)
    if status != 200:
        raise GithubError(f"GitHub answered {status}")
    try:
        login = json.loads(body).get("login")
    except (ValueError, AttributeError):
        raise GithubError("GitHub's answer was not a user") from None
    if not isinstance(login, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?", login
    ):
        raise GithubError("GitHub's answer had no login")
    return login


# --- one issue's details, for a question run (#273) -------------------------------------------

ISSUE_TITLE_LENGTH = 120
MAX_ISSUE_BYTES = 256 * 1024
MAX_LABELS = 20


def issue_details_from(item: object) -> IssueDetails | None:
    """The fields a question may see, or None for a pull request or something unexpected."""
    if not isinstance(item, dict) or "pull_request" in item:
        return None
    raw_title = item.get("title")
    try:
        title = clean_title(raw_title if isinstance(raw_title, str) else "", ISSUE_TITLE_LENGTH)
    except ValueError:
        title = f"#{item.get('number')}"
    labels: list[str] = []
    for label in item.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else None
        if isinstance(name, str) and is_plain_label(name) and name not in labels:
            labels.append(name)
        if len(labels) >= MAX_LABELS:
            break
    try:
        # Through JSON, so the strict model reads the timestamp as GitHub sends it.
        return IssueDetails.model_validate_json(
            json.dumps(
                {
                    "number": item.get("number"),
                    "state": item.get("state"),
                    "state_reason": item.get("state_reason"),
                    "title": title,
                    "labels": labels,
                    "comments": item.get("comments"),
                    "updated_at": item.get("updated_at"),
                }
            )
        )
    except (ValidationError, TypeError, ValueError):
        raise GithubError("GitHub's issue had an unexpected shape") from None


async def issue_details(
    owner: str, name: str, number: int, token: str, *, http: httpx2.AsyncClient, api: str = API
) -> IssueDetails | None:
    """One issue's details, or None when there is no such issue (or it is a pull request).

    A rate limit raises :class:`RateLimited`, never None: it says nothing about
    whether the issue exists. A transferred issue's 301 isn't followed, so it
    raises :class:`GithubError`.
    """
    if not is_owner_name(owner) or not is_bare_repo_name(name) or not 1 <= number <= 10**9:
        raise GithubError("not a GitHub issue")
    url = f"{api}/repos/{owner}/{name}/issues/{number}"
    status, body = await _page(http, url, token, MAX_ISSUE_BYTES)
    if status in (404, 410):
        return None
    if status != 200 or len(body) > MAX_ISSUE_BYTES:
        raise GithubError(f"GitHub answered {status}")
    try:
        item = json.loads(body)
    except ValueError:
        raise GithubError("GitHub's answer was not JSON") from None
    details = issue_details_from(item)
    if details is not None and details.number != number:
        raise GithubError("GitHub's answer was for another issue")
    return details
