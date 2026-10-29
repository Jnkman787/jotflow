"""JotFlow's side of the agent runners (#21, D6).

Each agent service runs a runner (#54). JotFlow asks it to start a run with
exactly the run id, run kind, and run token, follows the run with ``GET``, and
stops it with ``DELETE``. Runs are routed by kind, with no fallback:

- drafting and question runs go to the ``agent`` runner, with
  ``JOTFLOW_RUNNER_SECRET``
- execution runs go to the ``agent-exec`` runner, with
  ``JOTFLOW_RUNNER_EXEC_SECRET``

A secret is only ever sent to its own runner. The client never follows a
redirect and ignores proxy settings from the environment, so neither the secret
nor the token can be carried to another host. It resolves each runner's name
once, refuses unless every address is private (#234, :mod:`jotflow.netcheck`),
and connects to the address it checked, keeping the name in the Host header. A
runner that isn't running can't send the secret to a public host that a DNS
search domain points the name at.

:class:`RunSupervisor` drives runs from start to end through
:class:`~jotflow.orchestrator.runs.RunLifecycle`, so every run loses its token
as it ends. When a run fails or times out, it posts a short notice in the run's
thread. The notice is fixed text naming the run id only. It never carries the
agent's output, exit codes, or the runner's replies, which may quote Slack
text or carry details only an admin should see.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, TypeVar

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from jotflow.config import Settings
from jotflow.doc_paths import MAX_FILE_BYTES
from jotflow.models import (
    Account,
    Assignable,
    DiscoveredRepo,
    FoundBoard,
    IssueDetails,
    MarkedIssues,
    Membership,
    Run,
    RunKind,
    RunStatus,
    is_bare_repo_name,
    is_github_login,
    is_owner_name,
    under_owner,
)
from jotflow.netcheck import AddressRefused, Resolver, SystemResolver, checked_address
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.store.base import Store
from jotflow.store.titles import clean_title
from jotflow.whiteboard.write_tools import PostError, ThreadPoster

log = logging.getLogger("jotflow.launch")

REQUEST_TIMEOUT_SECONDS = 10.0
# A runner on the Compose network answers a connect at once; a dead one shouldn't
# hold a call for the full request timeout (#234).
CONNECT_TIMEOUT_SECONDS = 3.0
ALLOW_PUBLIC_ENV = "JOTFLOW_RUNNER_ALLOW_PUBLIC"
TICK_SECONDS = 2.0
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

FAILED_NOTICE = (
    "JotFlow's agent run for this thread did not finish (run `{run_id}`). "
    "An admin can find the reason in JotFlow's logs."
)
TIMED_OUT_NOTICE = (
    "JotFlow's agent run for this thread ran out of time and was stopped (run `{run_id}`)."
)

# Fixed reasons, stored on the run and logged. Never the runner's reply.
NO_RUNNER = "no runner secret is configured for this run kind"
UNREACHABLE = "the runner could not be reached"
LOST = "the runner lost the run"
NONZERO_EXIT = "the agent exited with a non-zero code"
STOPPED = "the runner stopped the run"
NOT_STARTED = "the runner could not start the agent"
_REFUSALS = {
    400: "the runner refused the request as malformed (400)",
    401: "the runner refused JotFlow's secret (401)",
    403: "the runner does not serve this run kind (403)",
    409: "the runner already has this run (409)",
    413: "the runner refused the request as too large (413)",
    502: "the runner won't hand the agent JotFlow's MCP URL: it doesn't resolve to a private "
    "address (502)",
    429: "the runner is at its limit of live runs (429)",
    503: "the runner has no GitHub token for this run kind (503)",
}


class RunnerError(Exception):
    """A runner call did not succeed. The message is one of the fixed reasons.

    ``retry`` says whether trying again could help: ``"unreachable"`` for a
    runner that may still be starting, ``"unresolved"`` for a name that doesn't
    resolve yet, or None for a refusal that won't fix itself.
    """

    def __init__(self, reason: str, *, retry: str | None = None) -> None:
        super().__init__(reason)
        self.retry = retry


class GithubBusy(RunnerError):
    """GitHub couldn't be asked right now: rate limited or failing (#273).

    Never a "not found": it says nothing about whether the issue exists.
    """


class NotThisProposal(RunnerError):
    """The runner found the recorded issue lacks the proposal's marker (#309)."""

    def __init__(self) -> None:
        super().__init__("the recorded issue isn't this proposal's")


def _refused(status: int) -> RunnerError:
    return RunnerError(_REFUSALS.get(status, f"the runner answered {status}"))


class RunnerState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    KILLED = "killed"
    FAILED = "failed"


LIVE_STATES = frozenset({RunnerState.STARTING, RunnerState.RUNNING})


class RunnerStatus(BaseModel):
    """The runner's view of a run, validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    state: RunnerState
    exit_code: int | None = None


class RunnerInfo(BaseModel):
    """What a runner says it starts, validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    preset: str = Field(max_length=64)
    kinds: tuple[RunKind, ...] = Field(max_length=len(RunKind))


class VerifyAnswer(BaseModel):
    """The drafting runner's answer to ``POST /verify`` (#195), validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    verified: bool
    rate_limited: bool
    github_id: int | None = Field(default=None, ge=1)
    login: str | None = Field(default=None, max_length=39)


# What the drafting runner may send back for repo docs (#209).
MAX_DOC_PATHS = 10_000
# 10,000 paths of up to 400 characters, as JSON.
MAX_DOCS_REPLY_BYTES = 5 * 1024 * 1024


class DocsListAnswer(BaseModel):
    """The drafting runner's answer to ``POST /docs/list`` (#209), validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    paths: list[str] = Field(max_length=MAX_DOC_PATHS)
    truncated: bool


class DocsReadAnswer(BaseModel):
    """The drafting runner's answer to ``POST /docs/read`` (#209), validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=400)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    text: str = Field(max_length=MAX_FILE_BYTES)
    truncated: bool


class DocMissing(RunnerError):
    """The path isn't a readable text file in the repo's default branch."""


class RunnerInfoWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    preset: str = Field(max_length=64)
    kinds: list[str] = Field(max_length=len(RunKind))


@dataclass(frozen=True)
class RunnerEndpoint:
    """One runner: where it listens and the secret only it receives."""

    url: str
    secret: str = field(repr=False)


# What the drafting runner's discovery may send back (#236).
MAX_DISCOVERED = 1000
MAX_DISCOVERY_BYTES = 2_000_000


class BoardsFoundWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boards: list[FoundBoard] = Field(max_length=500)


class DiscoveryWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[DiscoveredRepo] = Field(max_length=MAX_DISCOVERED)
    dropped: int = Field(ge=0)
    truncated: bool


class AccessWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reachable: bool


class BoardAccessWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    can_update: bool


class BoardAddedWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: str = Field(min_length=1, max_length=200)


# The largest marked-issue list JotFlow reads from a runner (#256).
MAX_MARKED_BYTES = 8_000_000
# The largest answer about one person (#305).
MAX_PERSON_BYTES = 4 * 1024
_Person = TypeVar("_Person", Account, Assignable, Membership)
# The largest single issue's details (#273).
MAX_ISSUE_BYTES = 64 * 1024
# The drafting runner's reply when GitHub has no such issue (#273).
NO_SUCH_ISSUE_REPLY = {"error": "no such issue"}


def _json_or_none(content: bytes) -> object:
    if len(content) > 1024:
        return None
    try:
        return json.loads(content)
    except ValueError:
        return None


class IdentityWire(BaseModel):
    """The execution runner's answer to GET /identity (#256)."""

    model_config = ConfigDict(extra="forbid")

    login: str = Field(max_length=45)

    @field_validator("login")
    @classmethod
    def _login(cls, value: str) -> str:
        if not is_github_login(value):
            raise ValueError("not a GitHub login")
        return value


class RunnerClient:
    """Calls the runner that serves each run's kind."""

    def __init__(
        self,
        *,
        conversation: RunnerEndpoint | None,
        execution: RunnerEndpoint | None,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        allow_public: bool = False,
    ) -> None:
        self._conversation = conversation
        self._execution = execution
        self._resolver: Resolver = resolver or SystemResolver()
        self._allow_public = allow_public
        self._http = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> RunnerClient:
        def endpoint(url: str, secret: SecretStr | None) -> RunnerEndpoint | None:
            return RunnerEndpoint(url, secret.get_secret_value()) if secret is not None else None

        return cls(
            conversation=endpoint(settings.runner_url, settings.runner_secret),
            execution=endpoint(settings.runner_exec_url, settings.runner_exec_secret),
            transport=transport,
            resolver=resolver,
            allow_public=settings.runner_allow_public,
        )

    def endpoint_for(self, kind: RunKind) -> RunnerEndpoint | None:
        """The runner for ``kind``. Execution never falls back to the other, nor the reverse."""
        return self._execution if kind is RunKind.EXECUTION else self._conversation

    def serves(self, kind: RunKind) -> bool:
        return self.endpoint_for(kind) is not None

    async def launch(self, run_id: str, kind: RunKind, token: str) -> None:
        """Ask the runner to start the run. Raises :class:`RunnerError` unless it started."""
        response = await self._call(
            "POST",
            kind,
            "/runs",
            json={"run_id": _checked(run_id), "kind": kind.value, "token": token},
        )
        if response.status_code != 202:
            raise _refused(response.status_code)

    async def status(self, run_id: str, kind: RunKind) -> RunnerStatus | None:
        """The run's state on its runner, or None if the runner does not know it."""
        response = await self._call("GET", kind, f"/runs/{_checked(run_id)}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            status = RunnerStatus.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a run status") from None
        if status.run_id != run_id:
            raise RunnerError("the runner's reply was for another run")
        return status

    async def info(self, kind: RunKind) -> RunnerInfo:
        """What the runner for ``kind`` starts: its preset and the kinds it serves."""
        response = await self._call("GET", kind, "/info")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            # Strict except for the kinds, which arrive as JSON strings.
            raw = RunnerInfoWire.model_validate_json(response.content)
            return RunnerInfo(preset=raw.preset, kinds=tuple(RunKind(k) for k in raw.kinds))
        except (ValidationError, ValueError):
            raise RunnerError("the runner's reply was not its info") from None

    async def repos(self, owner: str) -> list[DiscoveredRepo]:
        """``owner``'s repos, from the drafting runner's own GitHub call (#236).

        Checked again here: a repo under another owner, or with a bad name, is dropped.
        """
        if not is_owner_name(owner):
            raise RunnerError("not a GitHub owner")
        response = await self._call("GET", RunKind.DRAFTING, f"/repos/{owner}")
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_DISCOVERY_BYTES:
            raise RunnerError("the runner's repo list was too large")
        try:
            raw = DiscoveryWire.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a repo list") from None
        return [r for r in raw.repos[:MAX_DISCOVERED] if under_owner(r.full_name, owner)]

    async def find_boards(
        self,
        owner: str,
        *,
        numbers: Sequence[int],
        marker: str | None,
        creator_ids: Sequence[int],
        exclude: Sequence[str],
    ) -> list[FoundBoard]:
        """``owner``'s boards in a channel's scope, from the drafting runner (#309 PR 2).

        The runner applies the scope. Checked again here: at most 500 boards, each
        number once.
        """
        if not is_owner_name(owner):
            raise RunnerError("not a GitHub owner")
        response = await self._call(
            "POST",
            RunKind.DRAFTING,
            "/boards/find",
            json={
                "owner": owner,
                "numbers": list(numbers),
                "marker": marker,
                "creator_ids": list(creator_ids),
                "exclude": list(exclude),
            },
        )
        if response.status_code == 429:
            raise RunnerError("GitHub's rate limit", retry="rate_limited")
        if response.status_code == 403:
            raise RunnerError("the read token can't list boards")
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_DISCOVERY_BYTES:
            raise RunnerError("the runner's board list was too large")
        try:
            raw = BoardsFoundWire.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a board list") from None
        seen: set[int] = set()
        boards = []
        for board in raw.boards:
            if board.number not in seen:
                seen.add(board.number)
                boards.append(board)
        return boards

    async def marked_issues(
        self, owner: str, name: str, *, since: date | None = None
    ) -> MarkedIssues:
        """``owner/name``'s issues carrying a JotFlow marker, from the drafting runner (#256).

        Pointers and a cleaned title only. Checked again here, strictly, and the
        title cleaned again: the runner is trusted with the read token, not with
        what GitHub said.
        """
        if not is_owner_name(owner) or not is_bare_repo_name(name):
            raise RunnerError("not a GitHub repo")
        body: dict[str, Any] = {"owner": owner, "name": name, "since": None}
        if since is not None:
            body["since"] = since.isoformat()
        response = await self._call("POST", RunKind.DRAFTING, "/repos/jotflow-issues", json=body)
        if response.status_code == 502:
            raise RunnerError("GitHub didn't answer the drafting runner")
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_MARKED_BYTES:
            raise RunnerError("the runner's issue list was too large")
        try:
            found = MarkedIssues.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a list of marked issues") from None
        cleaned = []
        for issue in found.issues:
            try:
                cleaned.append(issue.model_copy(update={"title": clean_title(issue.title)}))
            except ValueError:
                cleaned.append(issue.model_copy(update={"title": f"#{issue.number}"}))
        return found.model_copy(update={"issues": tuple(cleaned)})

    async def account(self, login: str) -> Account:
        """What GitHub says about ``login``, from the drafting runner (#305).

        Raises :class:`GithubBusy` for a rate limit or GitHub failing, never "missing".
        """
        if not is_github_login(login) or login.endswith("[bot]"):
            raise RunnerError("not a GitHub account")
        return await self._person(f"/accounts/{login}", Account, login)

    async def assignable(self, owner: str, name: str, login: str) -> Assignable:
        """Whether ``login`` can be assigned issues in ``owner/name``, and its account (#305)."""
        if not is_owner_name(owner) or not is_bare_repo_name(name):
            raise RunnerError("not a GitHub repo")
        if not is_github_login(login) or login.endswith("[bot]"):
            raise RunnerError("not a GitHub account")
        return await self._person(f"/repos/{owner}/{name}/assignable/{login}", Assignable, login)

    async def membership(self, org: str, login: str) -> Membership:
        """Whether ``login`` is a member of ``org``, as far as the read token sees (#305)."""
        if not is_owner_name(org):
            raise RunnerError("not a GitHub org")
        if not is_github_login(login) or login.endswith("[bot]"):
            raise RunnerError("not a GitHub account")
        return await self._person(f"/orgs/{org}/members/{login}", Membership, None)

    async def _person(self, path: str, model: type[_Person], login: str | None) -> _Person:
        response = await self._call("GET", RunKind.DRAFTING, path)
        if response.status_code in (502, 503):
            raise GithubBusy("GitHub couldn't be asked right now")
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_PERSON_BYTES:
            raise RunnerError("the runner's answer was too large")
        try:
            found = model.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not about a GitHub account") from None
        named = getattr(found, "login", None)
        if login is not None and named is not None and named.casefold() != login.casefold():
            raise RunnerError("the runner's reply was about another account")
        return found

    async def issue(self, owner: str, name: str, number: int) -> IssueDetails | None:
        """One issue's details from the drafting runner, or None if there's no such issue.

        Raises :class:`GithubBusy` for a rate limit or GitHub failing, never None.
        The reply is checked strictly, and the title cleaned again.
        """
        if not is_owner_name(owner) or not is_bare_repo_name(name) or not 1 <= number <= 10**9:
            raise RunnerError("not a GitHub issue")
        response = await self._call(
            "GET", RunKind.DRAFTING, f"/repos/{owner}/{name}/issues/{number}"
        )
        if response.status_code == 404:
            # Only the runner's own answer is a miss: a 404 for a missing route or
            # a runner without the read token says nothing about the issue.
            if _json_or_none(response.content) == NO_SUCH_ISSUE_REPLY:
                return None
            raise RunnerError("the runner can't look up issues")
        if response.status_code in (502, 503):
            raise GithubBusy("GitHub couldn't be asked right now")
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_ISSUE_BYTES:
            raise RunnerError("the runner's issue was too large")
        try:
            details = IssueDetails.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not an issue") from None
        if details.number != number:
            raise RunnerError("the runner's reply was for another issue")
        try:
            title = clean_title(details.title, 120)
        except ValueError:
            title = f"#{number}"
        return details.model_copy(update={"title": title})

    async def write_login(self) -> str:
        """The execution runner's write token's own login (#256)."""
        response = await self._call("GET", RunKind.EXECUTION, "/identity")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            return IdentityWire.model_validate_json(response.content).login
        except ValidationError:
            raise RunnerError("the runner's reply was not a login") from None

    async def write_reachable(self, owner: str, name: str) -> bool:
        """Whether the execution runner's write token can see ``owner/name`` (#236)."""
        if not is_owner_name(owner) or not is_bare_repo_name(name):
            raise RunnerError("not a GitHub repo")
        response = await self._call("GET", RunKind.EXECUTION, f"/repos/{owner}/{name}/access")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            return AccessWire.model_validate_json(response.content).reachable
        except ValidationError:
            raise RunnerError("the runner's reply was not an access answer") from None

    async def board_access(self, owner: str, number: int) -> bool:
        """Whether the execution runner's write token may update an org board (#309)."""
        if not is_owner_name(owner) or not 1 <= number <= 10**9:
            raise RunnerError("not a board")
        response = await self._call("GET", RunKind.EXECUTION, f"/boards/{owner}/{number}/access")
        if response.status_code == 429:
            raise RunnerError("GitHub's rate limit", retry="rate_limited")
        if response.status_code == 502:
            raise RunnerError("GitHub could not be asked", retry="github")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            return BoardAccessWire.model_validate_json(response.content).can_update
        except ValidationError:
            raise RunnerError("the runner's reply was not a board access answer") from None

    async def add_to_board(
        self, repo: str, issue_number: int, board: str, proposal_id: str
    ) -> None:
        """Ask the execution runner to add issue ``repo#issue_number`` to ``board`` (#309),
        if its body carries ``proposal_id``'s marker."""
        response = await self._call(
            "POST",
            RunKind.EXECUTION,
            "/boards/add",
            json={
                "repo": repo,
                "issue_number": issue_number,
                "board": board,
                "proposal_id": proposal_id,
            },
        )
        if response.status_code == 422:
            raise NotThisProposal
        if response.status_code == 429:
            raise RunnerError("GitHub's rate limit", retry="rate_limited")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            BoardAddedWire.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a board item") from None

    async def verify(self, login: str, code_digest: str) -> VerifyAnswer:
        """Ask the drafting runner whether ``login``'s GitHub account carries the code (#195).

        Only the code's digest is sent; the runner hashes what it finds and compares.

        Raises :class:`RunnerError` if the runner refuses, or GitHub didn't answer.
        """
        response = await self._call(
            "POST", RunKind.DRAFTING, "/verify", json={"login": login, "code_digest": code_digest}
        )
        if response.status_code == 502:
            raise RunnerError("GitHub didn't answer the check")
        if response.status_code != 200:
            raise _refused(response.status_code)
        try:
            answer = VerifyAnswer.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a verification answer") from None
        if answer.verified and (answer.github_id is None or answer.login is None):
            raise RunnerError("the runner's reply was not a verification answer")
        return answer

    async def docs_list(self, repo: str) -> DocsListAnswer:
        """The text files in ``repo``'s default branch, from the drafting runner (#209).

        JotFlow matches these against the repo's ``read_paths``; the runner doesn't.
        """
        response = await self._call("POST", RunKind.DRAFTING, "/docs/list", json={"repo": repo})
        if response.status_code == 502:
            raise RunnerError(
                "GitHub didn't list the repo's files: check the read token has Contents: Read"
            )
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_DOCS_REPLY_BYTES:
            raise RunnerError("the runner's file list was too large")
        try:
            return DocsListAnswer.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a file list") from None

    async def docs_read(self, repo: str, path: str) -> DocsReadAnswer:
        """One text file's content, from the drafting runner (#209). Untrusted."""
        response = await self._call(
            "POST", RunKind.DRAFTING, "/docs/read", json={"repo": repo, "path": path}
        )
        if response.status_code == 404:
            raise DocMissing("no such text file in the repo's default branch")
        if response.status_code == 502:
            raise RunnerError("GitHub didn't return the file")
        if response.status_code != 200:
            raise _refused(response.status_code)
        if len(response.content) > MAX_DOCS_REPLY_BYTES:
            raise RunnerError("the runner's reply was too large")
        try:
            answer = DocsReadAnswer.model_validate_json(response.content)
        except ValidationError:
            raise RunnerError("the runner's reply was not a file") from None
        if answer.path != path:
            raise RunnerError("the runner returned a different file")
        return answer

    async def stop(self, run_id: str, kind: RunKind) -> None:
        """Ask the runner to kill the run. A run it does not know is already gone."""
        response = await self._call("DELETE", kind, f"/runs/{_checked(run_id)}")
        if response.status_code not in (200, 404):
            raise _refused(response.status_code)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, kind: RunKind, path: str, *, json: object = None
    ) -> httpx2.Response:
        endpoint = self.endpoint_for(kind)
        if endpoint is None:
            raise RunnerError(NO_RUNNER)
        url = httpx2.URL(endpoint.url + path)
        host, port = url.host, url.port or (443 if url.scheme == "https" else 80)
        try:
            address = await checked_address(
                self._resolver, host, port, allow_public=self._allow_public, opt_in=ALLOW_PUBLIC_ENV
            )
        except AddressRefused as refused:
            reason = str(refused)
            log.warning("not calling the %s runner: %s", kind.value, reason)
            raise RunnerError(reason, retry="unresolved" if refused.unresolved else None) from None
        try:
            # Dial the address that was checked, never a second lookup, and keep the
            # name in Host for the runner's allowlist.
            return await self._http.request(
                method,
                url.copy_with(host=address),
                # TLS still checks the certificate against the name, not the address.
                extensions={"sni_hostname": host} if url.scheme == "https" else {},
                json=json,
                headers={"Authorization": f"Bearer {endpoint.secret}", "Host": url.netloc.decode()},
            )
        except httpx2.HTTPError:
            # Never the exception text: it can name the request.
            raise RunnerError(UNREACHABLE, retry="unreachable") from None

    def public_hosts(self) -> list[str]:
        """Runner hosts JotFlow may send secrets to although they could be public."""
        if not self._allow_public:
            return []
        hosts = [httpx2.URL(e.url).host for e in (self._conversation, self._execution) if e]
        return sorted(set(hosts))


def _checked(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise RunnerError("the run id is not one a runner accepts")
    return run_id


def notice_for(run: Run) -> str | None:
    """The thread notice for a run that has ended, or None if it succeeded."""
    if run.status is RunStatus.TIMED_OUT:
        return TIMED_OUT_NOTICE.format(run_id=run.id)
    if run.status is RunStatus.FAILED:
        return FAILED_NOTICE.format(run_id=run.id)
    return None


def outcome(status: RunnerStatus) -> tuple[RunStatus, str | None] | None:
    """How a run ends, given its runner's state, or None while it is still live."""
    if status.state in LIVE_STATES:
        return None
    if status.state is RunnerState.EXITED:
        if status.exit_code == 0:
            return RunStatus.SUCCEEDED, None
        return RunStatus.FAILED, NONZERO_EXIT
    if status.state is RunnerState.TIMED_OUT:
        return RunStatus.TIMED_OUT, None
    if status.state is RunnerState.KILLED:
        return RunStatus.FAILED, STOPPED
    return RunStatus.FAILED, NOT_STARTED


class RunSupervisor:
    """Starts queued runs on their runners and follows each to its end.

    One :meth:`tick` times out overdue runs, then polls live ones, then starts
    any that may start. :meth:`run` repeats it until stopped.
    """

    def __init__(
        self,
        lifecycle: RunLifecycle,
        store: Store,
        client: RunnerClient,
        poster: ThreadPoster,
        *,
        max_running: int,
        kinds: Collection[RunKind] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._kinds = frozenset(kinds) if kinds is not None else None
        self._store = store
        self._client = client
        self._poster = poster
        self._max_running = max_running
        self._live: dict[str, Run] = {}

    def tracked(self) -> list[str]:
        return list(self._live)

    async def tick(self) -> None:
        await self._lifecycle.queue_due_retries()
        await self._expire()
        await self._poll()
        await self._launch()

    async def run(self, stop: asyncio.Event, *, interval: float = TICK_SECONDS) -> None:
        """Tick every ``interval`` seconds until ``stop`` is set, then stop live runs."""
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.exception("run supervisor tick failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval)
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop every live run on its runner, for JotFlow's shutdown.

        The runs stay running in the store; startup recovery settles them, as
        after a crash. Their tokens live only in memory and end with JotFlow.
        """
        for run in list(self._live.values()):
            await self._stop(run)
        self._live.clear()

    async def _expire(self) -> None:
        for run_id in await self._lifecycle.expire_overdue():
            run = self._live.pop(run_id, None)
            if run is not None:
                await self._stop(run)
            ended = await self._store.get_run(run_id)
            if ended is not None:
                log.warning("run %s timed out", run_id)
                await self._notify(ended)

    async def _poll(self) -> None:
        for run in list(self._live.values()):
            try:
                status = await self._client.status(run.id, run.kind)
            except RunnerError as exc:
                # Its deadline still bounds it.
                log.warning("run %s: could not check its state (%s)", run.id, exc)
                continue
            if status is None:
                await self._end(run, RunStatus.FAILED, LOST)
                continue
            ended = outcome(status)
            if ended is not None:
                await self._end(run, *ended)

    async def _launch(self) -> None:
        while len(self._live) < self._max_running:
            try:
                started = await self._lifecycle.start_next(
                    max_running=self._max_running, kinds=self._kinds
                )
            except Exception:
                log.exception("could not start the next run")
                return
            if started is None:
                return
            run = started.run
            try:
                await self._client.launch(run.id, run.kind, started.token)
            except RunnerError as exc:
                await self._end(run, RunStatus.FAILED, str(exc))
                if str(exc) == UNREACHABLE:
                    # The request may have arrived before the connection broke.
                    # The token is already revoked; this frees the runner's slot.
                    await self._stop(run)
                continue
            self._live[run.id] = run
            log.info("run %s launched on its runner (%s)", run.id, run.kind.value)

    async def _end(self, run: Run, status: RunStatus, error: str | None) -> None:
        self._live.pop(run.id, None)
        if error is not None:
            log.warning("run %s %s: %s", run.id, status.value, error)
        ended = await self._lifecycle.finish(run.id, status, error=error)
        if ended is not None:
            await self._notify(ended)

    async def _stop(self, run: Run) -> None:
        try:
            await self._client.stop(run.id, run.kind)
        except RunnerError as exc:
            log.warning("run %s: could not stop it on its runner (%s)", run.id, exc)

    async def _notify(self, run: Run) -> None:
        text = notice_for(run)
        if text is None:
            return
        try:
            await self._poster.post(run.channel_id, run.thread_ts, text)
        except PostError as exc:
            log.warning("run %s: could not post the failure notice (%s)", run.id, exc)
