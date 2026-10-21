"""The runner's GitHub reads for `jotflow rebuild --from-github` (#256), against a fake GitHub."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters.github_issues import marked_issues, parse_issue, token_login
from jotflow.adapters.github_repos import GithubError
from tests.test_github_repos import FakeGithub, answering, runner_http
from tests.test_runner import READ, WRITE

SECRET_TEXT = "SENTINEL-issue-body-9c1e"
PERMALINK = "https://acme-co.slack.com/archives/C0IDEAS01/p1760781660000100"
THREADED = (
    "https://acme-co.slack.com/archives/C0IDEAS01/p1760781700000200"
    "?thread_ts=1760781660.000100&cid=C0IDEAS01"
)


def issue(
    n: int, *, marker: str | None = "abc123", login: str = "jotflow-bot", **extra: Any
) -> dict[str, Any]:
    footer = f"\n\n---\nRaised in Slack: {PERMALINK} {THREADED}\n"
    body = f"Body with {SECRET_TEXT}.{footer}"
    if marker:
        body += f"<!-- jotflow:proposal:{marker} -->"
    return {
        "number": n,
        "state": "open",
        "title": f"Progress bar {n}",
        "body": body,
        "user": {"login": login, "type": "User"},
        "labels": [{"name": "secret-label"}],
        "assignees": [{"login": "someone"}],
        **extra,
    }


def failing(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(500, json={"message": SECRET_TEXT})


def listing(items: list[dict[str, Any]]) -> Any:
    def answer(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        return httpx2.Response(200, json=items[(page - 1) * 100 : page * 100])

    return answer


# --- parsing --------------------------------------------------------------------------------


def test_a_marked_issue_becomes_pointers_and_a_cleaned_title_only() -> None:
    parsed = parse_issue(issue(42))
    assert parsed is not None
    assert (parsed.number, parsed.state, parsed.title, parsed.author) == (
        42,
        "open",
        "Progress bar 42",
        "jotflow-bot",
    )
    assert parsed.markers == ("abc123",)
    assert [(s.channel_id, s.ts, s.thread_ts) for s in parsed.sources] == [
        ("C0IDEAS01", "1760781660.000100", None),
        ("C0IDEAS01", "1760781700.000200", "1760781660.000100"),
    ]
    assert SECRET_TEXT not in parsed.model_dump_json()


@pytest.mark.parametrize(
    "item",
    [
        issue(1, marker=None),  # no marker
        issue(2, pull_request={"url": "x"}),  # a pull request
        {**issue(3), "body": None},
        {**issue(4), "user": None},
        {**issue(5), "number": 0},
        {**issue(6), "state": "merged"},
        "not an issue",
    ],
)
def test_anything_but_a_marked_issue_is_left_out(item: Any) -> None:
    assert parse_issue(item) is None


def test_a_marker_must_be_a_whole_line() -> None:
    inline = issue(7, marker=None)
    inline["body"] += "see <!-- jotflow:proposal:abc --> inline"
    assert parse_issue(inline) is None


def test_a_permalink_must_be_exactly_jotflows_shape() -> None:
    odd = issue(8)
    odd["body"] = (
        "<!-- jotflow:proposal:abc -->\n"
        "https://evil.example/archives/C0IDEAS01/p1760781660000100 "
        "https://acme-co.slack.com/archives/C0IDEAS01/p123"
    )
    parsed = parse_issue(odd)
    assert parsed is not None and parsed.sources == ()


# --- paging ---------------------------------------------------------------------------------


async def test_issues_are_paged_until_a_short_page_and_filtered() -> None:
    items = [issue(n) for n in range(1, 101)] + [issue(101), issue(102, marker=None)]
    fake = FakeGithub({"/repos/acme-co/web/issues": listing(items)})
    found = await marked_issues("acme-co", "web", READ, http=fake.client())
    assert [i.number for i in found.issues] == list(range(1, 102))
    assert found.truncated is False
    assert [r.url.params["page"] for r in fake.seen] == ["1", "2"]
    first = fake.seen[0].url.params
    assert (first["state"], first["per_page"]) == ("all", "100")
    assert fake.seen[0].headers["authorization"] == f"Bearer {READ}"


async def test_since_is_passed_to_github() -> None:
    fake = FakeGithub({"/repos/acme-co/web/issues": listing([])})
    await marked_issues("acme-co", "web", READ, since=date(2026, 9, 1), http=fake.client())
    assert fake.seen[0].url.params["since"] == "2026-09-01T00:00:00Z"


async def test_a_github_error_and_a_bad_name_raise() -> None:
    fake = FakeGithub({"/repos/acme-co/web/issues": failing})
    with pytest.raises(GithubError):
        await marked_issues("acme-co", "web", READ, http=fake.client())
    with pytest.raises(GithubError):
        await marked_issues("acme-co", "..", READ, http=fake.client())


async def test_the_write_tokens_login() -> None:
    fake = FakeGithub({"/user": answering({"login": "jotflow-bot", "id": 9})})
    assert await token_login(WRITE, http=fake.client()) == "jotflow-bot"
    bad = FakeGithub({"/user": answering({"login": "not a login!"})})
    with pytest.raises(GithubError):
        await token_login(WRITE, http=bad.client())


# --- the runner's endpoints -------------------------------------------------------------------


async def test_the_drafting_runner_serves_marked_issues_with_no_issue_text(tmp_path: Path) -> None:
    fake = FakeGithub({"/repos/acme-co/web/issues": listing([issue(42)])})
    async with runner_http(tmp_path, fake) as http:
        answer = await http.post(
            "/repos/jotflow-issues", json={"owner": "acme-co", "name": "web", "since": None}
        )
        assert answer.status_code == 200
        assert [i["number"] for i in answer.json()["issues"]] == [42]
        for secret in (SECRET_TEXT, "secret-label", "someone", READ):
            assert secret not in answer.text
        extra = await http.post(
            "/repos/jotflow-issues", json={"owner": "acme-co", "name": "web", "page": 2}
        )
        assert extra.status_code == 400
        assert (
            await http.post("/repos/jotflow-issues", json={"owner": "acme*", "name": "web"})
        ).status_code == 400
        assert (await http.get("/identity")).status_code == 404


async def test_the_execution_runner_serves_identity_and_not_issues(tmp_path: Path) -> None:
    fake = FakeGithub({"/user": answering({"login": "jotflow-bot"})})
    async with runner_http(tmp_path, fake, execution=True) as http:
        answer = await http.get("/identity")
        assert answer.json() == {"login": "jotflow-bot"} and WRITE not in answer.text
        assert (
            await http.post("/repos/jotflow-issues", json={"owner": "acme-co", "name": "web"})
        ).status_code == 404


async def test_github_failing_is_a_fixed_502(tmp_path: Path) -> None:
    fake = FakeGithub({"/repos/acme-co/web/issues": failing})
    async with runner_http(tmp_path, fake) as http:
        answer = await http.post("/repos/jotflow-issues", json={"owner": "acme-co", "name": "web"})
        assert answer.status_code == 502 and SECRET_TEXT not in answer.text
        assert json.loads(answer.text) == {"error": "GitHub could not list the issues"}
