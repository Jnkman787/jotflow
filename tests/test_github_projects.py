"""The execution runner's fixed GitHub Projects calls (#309), against a fake GraphQL API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters import github_projects, github_repos
from jotflow.adapters.github_projects import ADD_ITEM, BOARD_ID, ISSUE_ID, add_issue, board_access
from jotflow.adapters.github_repos import GithubError, RateLimited
from tests.test_github_repos import FakeGithub, runner_http
from tests.test_runner import WRITE

ISSUE_NODE = "I_kwDOexample42"
PID = "p1"
BODY = "People want a CSV export.\n\n---\n<!-- jotflow:proposal:p1 -->"
BOARD_NODE = "PVT_kwDOexample3"


BOT = "example-jotflow-bot"


def issue_with(body: str, author: str | None = BOT, viewer: str = BOT) -> dict[str, Any]:
    login = None if author is None else {"login": author}
    issue = {"id": ISSUE_NODE, "body": body, "author": login}
    return {"data": {"viewer": {"login": viewer}, "repository": {"issue": issue}}}


class FakeGraphql:
    """GitHub's GraphQL endpoint: answers each constant query from a table."""

    def __init__(self, **answers: Any) -> None:
        self.answers = {
            ISSUE_ID: issue_with(BODY),
            BOARD_ID: {
                "data": {"organization": {"projectV2": {"id": BOARD_NODE, "viewerCanUpdate": True}}}
            },
            ADD_ITEM: {
                "data": {
                    "addProjectV2ItemById": {
                        "item": {"id": "PVTI_item1", "content": {"id": ISSUE_NODE}}
                    }
                }
            },
        }
        self.answers.update(answers)
        self.seen: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.seen.append(body)
        answer = self.answers[body["query"]]
        if isinstance(answer, httpx2.Response):
            return answer
        return httpx2.Response(200, json=answer)

    def client(self) -> httpx2.AsyncClient:
        return github_repos.client(transport=httpx2.MockTransport(self))


async def test_an_issue_is_added_with_three_constant_queries_and_typed_variables() -> None:
    fake = FakeGraphql()
    item = await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())
    assert item == "PVTI_item1"
    assert [s["query"] for s in fake.seen] == [ISSUE_ID, BOARD_ID, ADD_ITEM]
    assert fake.seen[0]["variables"] == {"owner": "example-org", "name": "web", "number": 42}
    assert fake.seen[1]["variables"] == {"owner": "example-org", "number": 3}
    assert fake.seen[2]["variables"] == {"board": BOARD_NODE, "issue": ISSUE_NODE}


@pytest.mark.parametrize(
    ("repo", "number", "board"),
    [
        ("example-org/../x", 1, "example-org/3"),
        ("bad owner/web", 1, "example-org/3"),
        ("example-org/web", 0, "example-org/3"),
        ("example-org/web", 10**9 + 1, "example-org/3"),
        ("example-org/web", 1, "example-org/x"),
        ("example-org/web", 1, "bad owner/3"),
        ("example-org/web", 1, "example-org/0"),
    ],
)
async def test_input_is_checked_before_anything_is_sent(repo: str, number: int, board: str) -> None:
    fake = FakeGraphql()
    with pytest.raises(GithubError):
        await add_issue(repo, number, board, PID, WRITE, http=fake.client())
    assert fake.seen == []


@pytest.mark.parametrize(
    ("query", "answer", "problem"),
    [
        (ISSUE_ID, {"errors": [{"message": "Could not resolve"}], "data": None}, "with errors"),
        (ISSUE_ID, {"data": {"repository": {"issue": None}}}, "null node"),
        (BOARD_ID, {"data": {"organization": {"projectV2": None}}}, "null node"),
        (
            BOARD_ID,
            {"data": {"organization": {"projectV2": {"id": BOARD_NODE, "viewerCanUpdate": False}}}},
            "can't update",
        ),
        (
            ADD_ITEM,
            {
                "data": {
                    "addProjectV2ItemById": {
                        "item": {"id": "PVTI_x", "content": {"id": "I_someone_else"}}
                    }
                }
            },
            "other than the issue",
        ),
        (ADD_ITEM, {"data": {"addProjectV2ItemById": {"item": None}}}, "null node"),
        (ADD_ITEM, httpx2.Response(502, text="bad gateway"), "answered 502"),
        (ADD_ITEM, httpx2.Response(200, text="not json"), "not JSON"),
        (ADD_ITEM, httpx2.Response(200, text="x" * (70 * 1024)), "too large"),
    ],
)
async def test_any_other_answer_fails(query: str, answer: Any, problem: str) -> None:
    fake = FakeGraphql()
    fake.answers[query] = answer
    with pytest.raises(GithubError, match=problem):
        await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())


async def test_a_rate_limit_is_its_own_failure() -> None:
    fake = FakeGraphql()
    fake.answers[ISSUE_ID] = httpx2.Response(403, headers={"x-ratelimit-remaining": "0"})
    with pytest.raises(RateLimited):
        await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())


async def test_access_means_the_token_may_update_the_board() -> None:
    fake = FakeGraphql()
    assert await board_access("example-org", 3, WRITE, http=fake.client())
    fake.answers[BOARD_ID] = {
        "data": {"organization": {"projectV2": {"id": BOARD_NODE, "viewerCanUpdate": False}}}
    }
    assert not await board_access("example-org", 3, WRITE, http=fake.client())
    fake.answers[BOARD_ID] = {"data": {"organization": {"projectV2": None}}}
    assert not await board_access("example-org", 3, WRITE, http=fake.client())
    with pytest.raises(GithubError):
        await board_access("example-org", 0, WRITE, http=fake.client())


async def test_the_execution_runner_serves_the_board_routes(tmp_path: Path) -> None:
    graphql = FakeGraphql()
    fake = FakeGithub({"/graphql": graphql})
    async with runner_http(tmp_path, fake, execution=True) as http:
        added = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 42,
                "board": "example-org/3",
                "proposal_id": PID,
            },
        )
        assert added.status_code == 200 and added.json() == {"item_id": "PVTI_item1"}
        access = await http.get("/boards/example-org/3/access")
        assert access.json() == {"can_update": True}
        assert (await http.get("/boards/bad%20owner/3/access")).status_code == 400
        loose = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": "42",
                "board": "example-org/3",
                "proposal_id": PID,
            },
        )
        assert loose.status_code == 400  # strict: no string numbers, no extra keys
        extra = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 42,
                "board": "example-org/3",
                "proposal_id": PID,
                "x": 1,
            },
        )
        assert extra.status_code == 400
        graphql.answers[ADD_ITEM] = {"data": {"addProjectV2ItemById": {"item": None}}}
        failed = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 42,
                "board": "example-org/3",
                "proposal_id": PID,
            },
        )
        assert failed.status_code == 502 and WRITE not in failed.text
    assert all(r.headers["Authorization"] == f"Bearer {WRITE}" for r in fake.seen)


async def test_the_drafting_runner_has_no_board_routes(tmp_path: Path) -> None:
    async with runner_http(tmp_path, FakeGithub({})) as http:
        assert (await http.get("/boards/example-org/3/access")).status_code == 404
        answer = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 42,
                "board": "example-org/3",
                "proposal_id": PID,
            },
        )
        assert answer.status_code == 404


def test_the_queries_are_constant_strings() -> None:
    for query in (ISSUE_ID, BOARD_ID, ADD_ITEM):
        assert "{" in query and "$" in query and "%" not in query
    assert github_projects.MAX_NUMBER == 10**9


# A real no, against a check that failed (#311 review)

NOT_FOUND = {
    "data": {"organization": {"projectV2": None}},
    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a ProjectV2"}],
}


@pytest.mark.parametrize(
    "answer",
    [
        NOT_FOUND,
        {"data": {"organization": None}, "errors": [{"type": "FORBIDDEN", "message": "no"}]},
        # A classic token without the project scope: a real no, not a failed check.
        {"data": None, "errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs project"}]},
        {"data": {"organization": {"projectV2": None}}},
        {"data": {"organization": None}},
        {"data": {"organization": {"projectV2": {"id": BOARD_NODE, "viewerCanUpdate": False}}}},
    ],
)
async def test_a_board_the_token_cant_see_or_update_is_a_no(answer: Any) -> None:
    fake = FakeGraphql()
    fake.answers[BOARD_ID] = answer
    assert await board_access("example-org", 3, WRITE, http=fake.client()) is False


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(502, text="bad gateway"),
        httpx2.Response(200, text="not json"),
        {"errors": [{"type": "INTERNAL", "message": "Something went wrong"}], "data": None},
        {"errors": [{"type": "NOT_FOUND"}, {"type": "INTERNAL"}], "data": None},
        {"data": {"organization": {"projectV2": {"id": BOARD_NODE}}}},
    ],
)
async def test_a_check_that_fails_is_no_answer_not_a_no(answer: Any) -> None:
    fake = FakeGraphql()
    fake.answers[BOARD_ID] = answer
    with pytest.raises(GithubError) as raised:
        await board_access("example-org", 3, WRITE, http=fake.client())
    assert not isinstance(raised.value, github_projects.Denied)


async def test_a_rate_limited_check_is_its_own_answer(tmp_path: Path) -> None:
    graphql = FakeGraphql()
    graphql.answers[BOARD_ID] = httpx2.Response(403, headers={"x-ratelimit-remaining": "0"})
    with pytest.raises(RateLimited):
        await board_access("example-org", 3, WRITE, http=graphql.client())
    async with runner_http(tmp_path, FakeGithub({"/graphql": graphql}), execution=True) as http:
        assert (await http.get("/boards/example-org/3/access")).status_code == 429
        graphql.answers[BOARD_ID] = httpx2.Response(502, text="bad gateway")
        assert (await http.get("/boards/example-org/3/access")).status_code == 502
        graphql.answers[BOARD_ID] = NOT_FOUND
        answer = await http.get("/boards/example-org/3/access")
        assert answer.status_code == 200 and answer.json() == {"can_update": False}


async def test_an_add_to_a_board_the_token_cant_see_still_fails() -> None:
    fake = FakeGraphql()
    fake.answers[BOARD_ID] = NOT_FOUND
    with pytest.raises(GithubError):
        await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())


# The issue must be the proposal's own (#309, security's addendum)


@pytest.mark.parametrize(
    "body",
    [
        "Someone else's issue, with no marker at all.",
        "Another proposal's.\n<!-- jotflow:proposal:p2 -->",
        "A prefix isn't it.\n<!-- jotflow:proposal:p10 -->",
        "Quoted mid-line: <!-- jotflow:proposal:p1 --> isn't a marker line",
        "",
    ],
)
async def test_an_issue_without_this_proposals_marker_line_is_never_added(body: str) -> None:
    fake = FakeGraphql()
    fake.answers[ISSUE_ID] = issue_with(body)
    with pytest.raises(github_projects.NotThisProposals):
        await add_issue("example-org/web", 5, "example-org/3", PID, WRITE, http=fake.client())
    assert [s["query"] for s in fake.seen] == [ISSUE_ID]  # no board lookup, no add


async def test_the_marker_line_may_end_in_a_carriage_return_among_other_markers() -> None:
    fake = FakeGraphql()
    fake.answers[ISSUE_ID] = issue_with(
        "Text\r\n<!-- jotflow:proposal:p0 -->\r\n<!-- jotflow:proposal:p1 -->\r\n"
    )
    item = await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())
    assert item == "PVTI_item1"


async def test_a_long_body_is_read_in_full_and_a_bad_proposal_id_sends_nothing() -> None:
    fake = FakeGraphql()
    fake.answers[ISSUE_ID] = issue_with("\u00e9" * 65_000 + "\n<!-- jotflow:proposal:p1 -->")
    assert await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())
    fresh = FakeGraphql()
    for bad in ("", "p 1", "x" * 65, "p1 -->"):
        with pytest.raises(GithubError, match="not a proposal id"):
            await add_issue("example-org/web", 42, "example-org/3", bad, WRITE, http=fresh.client())
    assert fresh.seen == []


async def test_the_runner_refuses_another_proposals_issue_with_a_fixed_answer(
    tmp_path: Path,
) -> None:
    graphql = FakeGraphql()
    graphql.answers[ISSUE_ID] = issue_with("Issue #5.\n<!-- jotflow:proposal:p2 -->")
    fake = FakeGithub({"/graphql": graphql})
    async with runner_http(tmp_path, fake, execution=True) as http:
        refused = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 5,
                "board": "example-org/3",
                "proposal_id": PID,
            },
        )
        assert refused.status_code == 422
        assert refused.json() == {"error": "the issue isn't this proposal's"}
        unnamed = await http.post(
            "/boards/add",
            json={"repo": "example-org/web", "issue_number": 5, "board": "example-org/3"},
        )
        assert unnamed.status_code == 400  # the proposal id is required
        loose = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 5,
                "board": "example-org/3",
                "proposal_id": "../x",
            },
        )
        assert loose.status_code == 400
    assert [json.loads(r.content)["query"] for r in fake.seen] == [ISSUE_ID]


# The issue must be one the write token filed (#311 review, #256's rule)


@pytest.mark.parametrize("author", ["example-alice", None])
async def test_an_issue_with_the_marker_but_another_author_is_never_added(
    author: str | None,
) -> None:
    # A collaborator pasted the marker into their own issue, or its author is gone.
    fake = FakeGraphql()
    fake.answers[ISSUE_ID] = issue_with(BODY, author=author)
    with pytest.raises(github_projects.NotThisProposals, match="wasn't filed by the write token"):
        await add_issue("example-org/web", 5, "example-org/3", PID, WRITE, http=fake.client())
    assert [s["query"] for s in fake.seen] == [ISSUE_ID]  # no board lookup, no add


async def test_the_author_matches_the_write_token_ignoring_case() -> None:
    fake = FakeGraphql()
    fake.answers[ISSUE_ID] = issue_with(BODY, author="Example-JotFlow-Bot")
    assert await add_issue("example-org/web", 42, "example-org/3", PID, WRITE, http=fake.client())
    assert "viewer { login }" in ISSUE_ID and "author { login }" in ISSUE_ID


async def test_the_runner_refuses_another_authors_issue_with_the_same_fixed_answer(
    tmp_path: Path,
) -> None:
    graphql = FakeGraphql()
    graphql.answers[ISSUE_ID] = issue_with(BODY, author="example-alice")
    async with runner_http(tmp_path, FakeGithub({"/graphql": graphql}), execution=True) as http:
        refused = await http.post(
            "/boards/add",
            json={
                "repo": "example-org/web",
                "issue_number": 5,
                "board": "example-org/3",
                "proposal_id": PID,
            },
        )
    assert refused.status_code == 422
    assert refused.json() == {"error": "the issue isn't this proposal's"}
