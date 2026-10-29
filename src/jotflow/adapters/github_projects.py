"""The execution runner's own GitHub Projects calls (#309). Never the agent's.

No agent gets a projects tool. GitHub MCP's ``projects_write`` can also delete
items, create boards and change views, and a preset can allow only whole tools,
while the token's Projects permission reaches every board of the organisation.
So after JotFlow records the issue an execution run wrote, it asks the runner
to add it to each approved board, and these three constant GraphQL queries do
it with the write token:

- :data:`ISSUE_ID`: the issue's node id, from its repo and number
- :data:`BOARD_ID`: an organisation board's node id, and whether the token may
  update it
- :data:`ADD_ITEM`: ``addProjectV2ItemById``, which returns the item that is
  already there when the issue is on the board, so a retry is harmless

Only typed variables come from outside: an owner checked by ``is_owner_name``,
a repo name by ``is_bare_repo_name``, and numbers from 1 to 10^9. An answer
with any ``errors``, a null node, or another shape is a failure, and the add
must return an item whose content is the issue looked up (security's
conditions). Reading is capped, redirects are never followed, and no proxy is
taken from the environment, as in :mod:`jotflow.adapters.github_repos`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jotflow.adapters.github_repos import GithubError, RateLimited, _headers, _rate_limited
from jotflow.models import is_bare_repo_name, is_owner_name

GRAPHQL = "https://api.github.com/graphql"
MAX_NUMBER = 10**9
MAX_BYTES = 64 * 1024

# Constant queries: only the variables below ever change.
ISSUE_ID = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) { issue(number: $number) { id } } }"
)
BOARD_ID = (
    "query($owner: String!, $number: Int!) {"
    " organization(login: $owner) { projectV2(number: $number) { id viewerCanUpdate } } }"
)
ADD_ITEM = (
    "mutation($board: ID!, $issue: ID!) {"
    " addProjectV2ItemById(input: {projectId: $board, contentId: $issue}) {"
    " item { id content { ... on Issue { id } } } } }"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Node(_Strict):
    id: str = Field(min_length=1, max_length=200)


class _IssueHolder(_Strict):
    issue: _Node


class _IssueData(_Strict):
    repository: _IssueHolder


class _Board(_Node):
    viewerCanUpdate: bool


class _BoardHolder(_Strict):
    projectV2: _Board


class _MaybeBoardHolder(_Strict):
    projectV2: _Board | None


class _MaybeBoardData(_Strict):
    organization: _MaybeBoardHolder | None


class _BoardData(_Strict):
    organization: _BoardHolder


class _Item(_Strict):
    id: str = Field(min_length=1, max_length=200)
    content: _Node


class _Added(_Strict):
    item: _Item


class _AddData(_Strict):
    addProjectV2ItemById: _Added


# GraphQL error types that answer the question: the token can't see it.
_DENIED = frozenset({"NOT_FOUND", "FORBIDDEN"})


class Denied(GithubError):
    """GitHub answered that the token can't see what was asked for: a real no."""


def _check_owner(owner: str) -> None:
    if not is_owner_name(owner):
        raise GithubError("not a GitHub owner")


def _check_number(number: int) -> None:
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_NUMBER:
        raise GithubError("not a number from 1 to 1,000,000,000")


async def _ask(
    http: httpx2.AsyncClient,
    token: str,
    query: str,
    variables: dict[str, Any],
    model: type[_Strict],
    *,
    url: str,
) -> Any:
    """One GraphQL call, read with a cap, and its ``data`` validated by ``model``."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {**_headers(token), "Content-Type": "application/json"}
    async with http.stream("POST", url, headers=headers, content=body) as response:
        if _rate_limited(response):
            raise RateLimited("GitHub's rate limit")
        raw = b""
        async for chunk in response.aiter_bytes():
            raw += chunk
            if len(raw) > MAX_BYTES:
                raise GithubError("GitHub's answer was too large")
        status = response.status_code
    if status != 200:
        raise GithubError(f"GitHub answered {status}")
    try:
        answer = json.loads(raw)
    except ValueError:
        raise GithubError("GitHub's answer was not JSON") from None
    if not isinstance(answer, dict):
        raise GithubError("GitHub's answer was not an object")
    if errors := answer.get("errors"):
        if isinstance(errors, list) and all(
            isinstance(e, dict) and e.get("type") in _DENIED for e in errors
        ):
            raise Denied("GitHub answered that the token can't see it")
        raise GithubError("GitHub answered with errors")
    try:
        return model.model_validate(answer.get("data"))
    except ValidationError:
        raise GithubError("GitHub's answer had an unexpected shape, or a null node") from None


async def board_access(
    owner: str, number: int, token: str, *, http: httpx2.AsyncClient, url: str = GRAPHQL
) -> bool:
    """Whether ``token`` may update the organisation board ``owner``'s ``number``.

    Seeing a board isn't enough: the add needs to change it. A board the token
    can't see comes back as a null node, or as a not-found or forbidden error,
    which is a no. Anything else, such as a GitHub outage or an odd answer, is
    no answer at all: it raises :class:`GithubError`, or :class:`RateLimited`,
    so a passing failure never reads as a missing permission (#311 review).
    """
    _check_owner(owner)
    _check_number(number)
    try:
        data: _MaybeBoardData = await _ask(
            http, token, BOARD_ID, {"owner": owner, "number": number}, _MaybeBoardData, url=url
        )
    except Denied:
        return False
    if data.organization is None or data.organization.projectV2 is None:
        return False
    return data.organization.projectV2.viewerCanUpdate


async def add_issue(
    repo: str,
    issue_number: int,
    board: str,
    token: str,
    *,
    http: httpx2.AsyncClient,
    url: str = GRAPHQL,
) -> str:
    """Add issue ``repo#issue_number`` to the organisation board ``owner/number``.

    Returns the board item's id. Raises :class:`GithubError` on anything else.
    """
    owner, _, name = repo.partition("/")
    _check_owner(owner)
    if not is_bare_repo_name(name):
        raise GithubError("not a GitHub repo")
    _check_number(issue_number)
    board_owner, _, number_text = board.partition("/")
    _check_owner(board_owner)
    if not number_text.isdigit():
        raise GithubError("a board is owner/number")
    number = int(number_text)
    _check_number(number)
    issue: _IssueData = await _ask(
        http,
        token,
        ISSUE_ID,
        {"owner": owner, "name": name, "number": issue_number},
        _IssueData,
        url=url,
    )
    found: _BoardData = await _ask(
        http, token, BOARD_ID, {"owner": board_owner, "number": number}, _BoardData, url=url
    )
    target = found.organization.projectV2
    if not target.viewerCanUpdate:
        raise GithubError("the write token can't update that board")
    added: _AddData = await _ask(
        http,
        token,
        ADD_ITEM,
        {"board": target.id, "issue": issue.repository.issue.id},
        _AddData,
        url=url,
    )
    item = added.addProjectV2ItemById.item
    if item.content.id != issue.repository.issue.id:
        raise GithubError("GitHub added something other than the issue")
    return item.id
