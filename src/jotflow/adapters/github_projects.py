"""The execution runner's own GitHub Projects calls (#309). Never the agent's.

No agent gets a projects tool. GitHub MCP's ``projects_write`` can also delete
items, create boards and change views, and a preset can allow only whole tools,
while the token's Projects permission reaches every board of the organisation.
So after JotFlow records the issue an execution run wrote, it asks the runner
to add it to each approved board, and these three constant GraphQL queries do
it with the write token:

- :data:`ISSUE_ID`: the issue's node id and body, from its repo and number.
  The body must carry this proposal's marker line, and the issue's author must
  be the token's own login, read in the same query as ``viewer``, so an
  execution agent can't report another issue in the approved repo and get that
  one boarded
- :data:`BOARD_ID`: an organisation board's node id, and whether the token may
  update it
- :data:`ADD_ITEM`: ``addProjectV2ItemById``, which returns the item that is
  already there when the issue is on the board, so a retry is harmless

Only typed variables come from outside: an owner checked by ``is_owner_name``,
a repo name by ``is_bare_repo_name``, numbers from 1 to 10^9, and a proposal
id by the marker's own pattern. An answer
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
from jotflow.execution.writes import PROPOSAL_ID, marker
from jotflow.models import FoundBoard, is_bare_repo_name, is_owner_name
from jotflow.policy import has_marker, title_excluded

GRAPHQL = "https://api.github.com/graphql"
MAX_NUMBER = 10**9
MAX_BYTES = 64 * 1024
# The issue's answer carries its body: up to 65,536 characters, JSON-escaped.
MAX_ISSUE_BYTES = 512 * 1024

# Constant queries: only the variables below ever change.
ISSUE_ID = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " viewer { login }"
    " repository(owner: $owner, name: $name) {"
    " issue(number: $number) { id body author { login } } } }"
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


class _Login(_Strict):
    login: str = Field(min_length=1, max_length=100)


class _Issue(_Node):
    body: str
    author: _Login | None  # null for a deleted account: never the write token's


class _IssueHolder(_Strict):
    issue: _Issue


class _IssueData(_Strict):
    viewer: _Login
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


# GraphQL error types that answer the question: the token can't see it, or a
# classic token lacks the project scope (#323 review).
_DENIED = frozenset({"NOT_FOUND", "FORBIDDEN", "INSUFFICIENT_SCOPES"})


class Denied(GithubError):
    """GitHub answered that the token can't see what was asked for: a real no."""


class NotThisProposals(GithubError):
    """The issue doesn't carry the proposal's marker line, so it isn't the one
    the approved proposal produced. Fixed: trying again won't change it."""

    def __init__(self, reason: str = "the issue doesn't carry this proposal's marker") -> None:
        super().__init__(reason)


def carries_marker(body: str, proposal_id: str) -> bool:
    """Whether ``body`` has ``proposal_id``'s marker as a whole line of its own."""
    wanted = marker(proposal_id)
    return any(line.rstrip("\r") == wanted for line in body.split("\n"))


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
    max_bytes: int = MAX_BYTES,
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
            if len(raw) > max_bytes:
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
    proposal_id: str,
    token: str,
    *,
    http: httpx2.AsyncClient,
    url: str = GRAPHQL,
) -> str:
    """Add issue ``repo#issue_number`` to the organisation board ``owner/number``,
    if the issue is the one proposal ``proposal_id`` produced.

    Returns the board item's id. Raises :class:`NotThisProposals` when the issue's
    body lacks the proposal's marker line, before the board is even looked up, and
    :class:`GithubError` on anything else.
    """
    if not PROPOSAL_ID.fullmatch(proposal_id):
        raise GithubError("not a proposal id")
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
        max_bytes=MAX_ISSUE_BYTES,
    )
    found_issue = issue.repository.issue
    if not carries_marker(found_issue.body, proposal_id):
        raise NotThisProposals
    # The marker shows the body names this proposal, not who wrote it: anyone who
    # may edit an issue in the repo could paste it. Only the write token files
    # JotFlow's issues (#256's rule, #311 review).
    author = found_issue.author.login if found_issue.author else None
    if author is None or author.casefold() != issue.viewer.login.casefold():
        raise NotThisProposals("the issue wasn't filed by the write token")
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


# Board discovery (#309 PR 2): one constant, paged query with the read token.
# It reads each board's number, title, short description, whether it is closed
# or a template, and who created it, never its items, fields or views.
FIND_BOARDS = (
    "query($owner: String!, $cursor: String) {"
    " organization(login: $owner) { projectsV2(first: 100, after: $cursor) {"
    " nodes { number title shortDescription closed template"
    " creator { __typename ... on User { databaseId } } }"
    " pageInfo { hasNextPage endCursor } } } }"
)
MAX_FIND_PAGES = 5
MAX_FIND_BYTES = 256 * 1024
MAX_CURSOR = 200


class _Creator(_Strict):
    typename: str = Field(alias="__typename", max_length=40)
    databaseId: int | None = None


class _FoundNode(_Strict):
    number: int = Field(ge=1, le=MAX_NUMBER)
    title: str = Field(max_length=1000)
    shortDescription: str | None = Field(default=None, max_length=4000)
    closed: bool
    template: bool
    creator: _Creator | None = None


class _PageInfo(_Strict):
    hasNextPage: bool
    endCursor: str | None = Field(default=None, max_length=MAX_CURSOR)


class _Boards(_Strict):
    nodes: list[_FoundNode | None] = Field(max_length=100)
    pageInfo: _PageInfo


class _BoardsHolder(_Strict):
    projectsV2: _Boards


class _FindData(_Strict):
    organization: _BoardsHolder


def _in_scope(
    node: _FoundNode,
    numbers: frozenset[int],
    marker: str | None,
    creator_ids: frozenset[int],
    exclude: tuple[str, ...],
) -> bool:
    """The scope, as policy defines it: by number, or by the marker on a board one
    of the listed people created. A bot, an app or a null creator never counts."""
    if node.closed or node.template or not node.title.strip():
        return False
    if title_excluded(node.title, exclude):
        return False
    if node.number in numbers:
        return True
    if marker is None or not has_marker(node.shortDescription, marker):
        return False
    creator = node.creator
    return (
        creator is not None
        and creator.typename == "User"
        and creator.databaseId is not None
        and creator.databaseId in creator_ids
    )


async def find_boards(
    owner: str,
    token: str,
    *,
    numbers: frozenset[int],
    marker: str | None,
    creator_ids: frozenset[int],
    exclude: tuple[str, ...],
    http: httpx2.AsyncClient,
    url: str = GRAPHQL,
) -> list[FoundBoard]:
    """The organisation ``owner``'s boards in the scope given, at most 500 looked at.

    The scope is applied here, in the runner, so an excluded board's title or
    creator never leaves it. A page past the cap, an answer over its size cap, or
    any other failure raises :class:`GithubError`: nothing partial is returned.
    """
    _check_owner(owner)
    found: list[FoundBoard] = []
    cursor: str | None = None
    for _ in range(MAX_FIND_PAGES):
        data: _FindData = await _ask(
            http,
            token,
            FIND_BOARDS,
            {"owner": owner, "cursor": cursor},
            _FindData,
            url=url,
            max_bytes=MAX_FIND_BYTES,
        )
        page = data.organization.projectsV2
        for node in page.nodes:
            if node is not None and _in_scope(node, numbers, marker, creator_ids, exclude):
                found.append(
                    FoundBoard(
                        number=node.number,
                        title=node.title[:256],
                        description=(node.shortDescription or "")[:1000] or None,
                    )
                )
        if not page.pageInfo.hasNextPage:
            return found
        cursor = page.pageInfo.endCursor
        if not cursor:
            raise GithubError("GitHub said there was another page but gave no cursor")
    raise GithubError(f"the organisation has more than {MAX_FIND_PAGES * 100} boards")
