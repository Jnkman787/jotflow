"""The drafting runner's person checks for assigning by request (#305), against a fake GitHub."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters.github_people import account, assignable, is_person_login, membership
from jotflow.adapters.github_repos import GithubError, RateLimited
from jotflow.models import Account, Assignable, Membership
from jotflow.orchestrator.launch import GithubBusy, RunnerClient, RunnerEndpoint, RunnerError
from tests.test_github_repos import FakeGithub, answering, runner_http
from tests.test_runner import READ
from tests.test_verify_client import SECRET

USER = {"login": "octo-example", "id": 583231, "type": "User", "email": "secret@example.com"}
ORG = {"login": "acme-co", "id": 9919, "type": "Organization"}


def status(code: int, **headers: str) -> Any:
    return lambda _: httpx2.Response(code, headers=headers)


def github(**routes: Any) -> FakeGithub:
    table = {"/users/octo-example": answering(USER), **routes}
    return FakeGithub(table)


# --- the GitHub calls ---------------------------------------------------------------------


async def test_an_account_is_a_user_or_organization_with_its_id_only() -> None:
    fake = github()
    found = await account("Octo-Example", READ, http=fake.client())
    assert found == Account(account="user", id=583231, login="octo-example")
    assert "secret@example.com" not in found.model_dump_json()
    assert fake.seen[0].headers["authorization"] == f"Bearer {READ}"
    org = FakeGithub({"/users/acme-co": answering(ORG)})
    assert (await account("acme-co", READ, http=org.client())).account == "organization"
    missing = FakeGithub({})
    assert await account("nobody-here", READ, http=missing.client()) == Account(account="missing")


@pytest.mark.parametrize(
    "body",
    [
        {**USER, "login": "someone-else"},  # another account
        {**USER, "type": "Bot"},
        {**USER, "id": "583231"},
        {**USER, "id": True},
        "not json",
    ],
)
async def test_an_odd_account_answer_raises(body: Any) -> None:
    fake = FakeGithub({"/users/octo-example": answering(body)})
    with pytest.raises(GithubError):
        await account("octo-example", READ, http=fake.client())


@pytest.mark.parametrize("login", ["dependabot[bot]", "-bad", "a" * 40, "../x", "a/b"])
async def test_a_bad_login_never_reaches_github(login: str) -> None:
    assert not is_person_login(login)
    fake = FakeGithub({})
    with pytest.raises(GithubError):
        await account(login, READ, http=fake.client())
    assert fake.seen == []


async def test_assignable_is_githubs_own_answer_for_that_repo() -> None:
    yes = github(**{"/repos/acme-co/web/assignees/octo-example": status(204)})
    found = await assignable("acme-co", "web", "octo-example", READ, http=yes.client())
    assert found == Assignable(account="user", id=583231, login="octo-example", assignable=True)
    no = github(**{"/repos/acme-co/web/assignees/octo-example": status(404)})
    refused = await assignable("acme-co", "web", "octo-example", READ, http=no.client())
    assert not refused.assignable


async def test_an_organization_or_missing_account_is_never_assignable() -> None:
    org = FakeGithub({"/users/acme-co": answering(ORG)})
    found = await assignable("acme-co", "web", "acme-co", READ, http=org.client())
    assert (found.account, found.assignable) == ("organization", False)
    assert [r.url.path for r in org.seen] == ["/users/acme-co"]  # no second call


@pytest.mark.parametrize(
    ("code", "headers", "member"),
    [
        (204, {}, "yes"),
        (404, {}, "no"),
        (302, {"location": "https://x"}, "hidden"),
        (403, {}, "hidden"),
    ],
)
async def test_membership_fails_closed_when_the_token_cant_see(
    code: int, headers: dict[str, str], member: str
) -> None:
    fake = FakeGithub({"/orgs/acme-co/members/octo-example": status(code, **headers)})
    found = await membership("acme-co", "octo-example", READ, http=fake.client())
    assert found == Membership(member=member)  # type: ignore[arg-type]
    assert len(fake.seen) == 1  # a redirect is never followed


async def test_a_rate_limit_is_never_a_no() -> None:
    limited = status(429)
    for call in (
        account("octo-example", READ, http=FakeGithub({"/users/octo-example": limited}).client()),
        membership(
            "acme-co",
            "octo-example",
            READ,
            http=FakeGithub({"/orgs/acme-co/members/octo-example": limited}).client(),
        ),
    ):
        with pytest.raises(RateLimited):
            await call


# --- the drafting runner's routes -----------------------------------------------------------


async def test_the_runner_answers_each_check_with_a_strict_body(tmp_path: Path) -> None:
    fake = github(
        **{
            "/repos/acme-co/web/assignees/octo-example": status(204),
            "/orgs/acme-co/members/octo-example": status(302, location="https://x"),
        }
    )
    async with runner_http(tmp_path, fake) as http:
        who = await http.get("/accounts/octo-example")
        can = await http.get("/repos/acme-co/web/assignable/octo-example")
        org = await http.get("/orgs/acme-co/members/octo-example")
    assert who.json() == {"account": "user", "id": 583231, "login": "octo-example"}
    assert can.json()["assignable"] is True
    assert org.json() == {"member": "hidden"}
    for answer in (who, can, org):
        assert READ not in answer.text and "secret@example.com" not in answer.text


@pytest.mark.parametrize(
    "path",
    [
        "/accounts/bad--",
        "/accounts/x[bot]",
        "/repos/acme*/web/assignable/octo",
        "/orgs/a_b/members/octo",
    ],
)
async def test_bad_names_are_a_400(tmp_path: Path, path: str) -> None:
    fake = FakeGithub({})
    async with runner_http(tmp_path, fake) as http:
        assert (await http.get(path)).status_code == 400
    assert fake.seen == []


async def test_github_trouble_is_503_or_502_never_missing(tmp_path: Path) -> None:
    limited = FakeGithub({"/users/octo-example": status(429)})
    async with runner_http(tmp_path, limited) as http:
        assert (await http.get("/accounts/octo-example")).status_code == 503
    broken = FakeGithub({"/users/octo-example": status(500)})
    async with runner_http(tmp_path, broken) as http:
        answer = await http.get("/accounts/octo-example")
    assert answer.status_code == 502 and answer.json() == {"error": "GitHub could not be asked"}


async def test_the_execution_runner_has_no_person_checks(tmp_path: Path) -> None:
    fake = github()
    async with runner_http(tmp_path, fake, execution=True) as http:
        assert (await http.get("/accounts/octo-example")).status_code == 404
    assert fake.seen == []


# --- JotFlow's client -----------------------------------------------------------------------


def client(code: int, body: Any) -> tuple[RunnerClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if isinstance(body, bytes):
            return httpx2.Response(code, content=body)
        return httpx2.Response(code, json=body)

    return (
        RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", SECRET),
            execution=RunnerEndpoint("http://agent-exec:8766", "e" * 40),
            transport=httpx2.MockTransport(handle),
        ),
        seen,
    )


async def test_the_client_reads_each_answer_strictly_from_the_drafting_runner() -> None:
    runner, seen = client(200, {"account": "user", "id": 5, "login": "octo-example"})
    assert (await runner.account("octo-example")).id == 5
    assert seen[0].headers["host"] == "agent:8766" and seen[0].url.path == "/accounts/octo-example"
    can, _ = client(200, {"account": "user", "id": 5, "login": "octo-example", "assignable": True})
    assert (await can.assignable("acme-co", "web", "octo-example")).assignable
    org, _ = client(200, {"member": "no"})
    assert (await org.membership("acme-co", "octo-example")).member == "no"


@pytest.mark.parametrize(
    "body",
    [
        {"account": "user", "id": "5", "login": "octo-example"},
        {"account": "user", "id": 5, "login": "octo-example", "email": "x"},
        {"account": "bot", "id": 5},
        {"account": "user", "id": 5, "login": "someone-else"},
        b"x" * 5000,
    ],
)
async def test_anything_off_shape_is_refused(body: Any) -> None:
    runner, _ = client(200, body)
    with pytest.raises(RunnerError) as raised:
        await runner.account("octo-example")
    assert not isinstance(raised.value, GithubBusy)


@pytest.mark.parametrize("code", [502, 503])
async def test_github_trouble_reads_as_busy(code: int) -> None:
    runner, _ = client(code, {"error": "x"})
    with pytest.raises(GithubBusy):
        await runner.account("octo-example")


async def test_bad_names_are_never_sent() -> None:
    runner, seen = client(200, {})
    for call in (
        runner.account("x[bot]"),
        runner.assignable("acme*", "web", "octo"),
        runner.assignable("acme-co", "..", "octo"),
        runner.membership("a_b", "octo"),
    ):
        with pytest.raises(RunnerError):
            await call
    assert seen == []


@pytest.mark.parametrize(
    "body",
    [
        {"account": "missing", "id": 5},
        {"account": "missing", "login": "octo-example"},
        {"account": "user", "id": 5},
        {"account": "organization", "login": "acme-co"},
    ],
)
def test_an_account_has_both_id_and_login_or_neither(body: dict[str, Any]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Account.model_validate(body)


def test_only_a_user_can_be_assignable() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Assignable(account="organization", id=1, login="acme-co", assignable=True)
    with pytest.raises(ValidationError):
        Assignable(account="missing", assignable=True)
