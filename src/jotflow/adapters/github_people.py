"""The drafting runner's GitHub reads about a person, for assigning by request (#305).

Three fixed calls, each with the read token, path parameters only, no redirects:

- ``GET /users/{login}``: whether the account exists, whether it's a person or an
  organization, and GitHub's numeric id, which survives a rename
- ``GET /repos/{owner}/{repo}/assignees/{login}``: whether GitHub would accept the
  account as an assignee in that repo, which is what an issue's assignee needs
- ``GET /orgs/{org}/members/{login}``: whether the account is a member of ``org``

A rate limit raises :class:`~jotflow.adapters.github_repos.RateLimited`, never a
"no": it says nothing about the person. Membership the token can't see, a
redirect to the public list or a refusal, is ``hidden``, so the caller fails
closed with a reason the admin can act on.
"""

from __future__ import annotations

import json

import httpx2
from pydantic import ValidationError

from jotflow.adapters.github_repos import API, GithubError, _page
from jotflow.models import (
    Account,
    Assignable,
    Membership,
    is_bare_repo_name,
    is_github_login,
    is_owner_name,
)

MAX_USER_BYTES = 64 * 1024


def is_person_login(value: str) -> bool:
    """A login a person could have: a GitHub login, never an app's ``name[bot]``."""
    return is_github_login(value) and not value.endswith("[bot]")


def _check(login: str, *owners: str) -> None:
    if not is_person_login(login) or not all(is_owner_name(o) for o in owners):
        raise GithubError("not a GitHub account")


async def account(login: str, token: str, *, http: httpx2.AsyncClient, api: str = API) -> Account:
    """The account ``login`` names, or ``missing``."""
    _check(login)
    status, body = await _page(http, f"{api}/users/{login}", token, MAX_USER_BYTES)
    if status == 404:
        return Account(account="missing")
    if status != 200:
        raise GithubError(f"GitHub answered {status}")
    try:
        data = json.loads(body)
        kind, number, name = data.get("type"), data.get("id"), data.get("login")
    except (ValueError, AttributeError):
        raise GithubError("GitHub's answer was not an account") from None
    if not isinstance(name, str) or name.casefold() != login.casefold():
        raise GithubError("GitHub's answer was for another account")
    if kind not in ("User", "Organization") or type(number) is not int:
        raise GithubError("GitHub's answer had no account type or id")
    try:
        return Account(account="user" if kind == "User" else "organization", id=number, login=name)
    except ValidationError:
        raise GithubError("GitHub's account had an unexpected shape") from None


async def assignable(
    owner: str, name: str, login: str, token: str, *, http: httpx2.AsyncClient, api: str = API
) -> Assignable:
    """The account, and whether it can be assigned in ``owner/name``. Only a user can."""
    _check(login, owner)
    if not is_bare_repo_name(name):
        raise GithubError("not a GitHub repo")
    found = await account(login, token, http=http, api=api)
    if found.account != "user":
        return Assignable(**found.model_dump())
    url = f"{api}/repos/{owner}/{name}/assignees/{found.login}"
    status, _ = await _page(http, url, token, 1024)
    if status not in (204, 404):
        raise GithubError(f"GitHub answered {status}")
    return Assignable(**found.model_dump(), assignable=status == 204)


async def membership(
    org: str, login: str, token: str, *, http: httpx2.AsyncClient, api: str = API
) -> Membership:
    """Whether ``login`` is a member of ``org``, as far as the read token can see.

    204 is a member and 404 is not. A 302 to the public list, or a 403, means the
    token can't see private memberships: ``hidden``, which the caller refuses.
    """
    _check(login, org)
    status, _ = await _page(http, f"{api}/orgs/{org}/members/{login}", token, 1024)
    if status == 204:
        return Membership(member="yes")
    if status == 404:
        return Membership(member="no")
    if status in (302, 403):
        return Membership(member="hidden")
    raise GithubError(f"GitHub answered {status}")
