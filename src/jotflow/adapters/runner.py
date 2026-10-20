"""The runner inside the agent container (#54, D6).

JotFlow never mounts the Docker socket. To start a run it asks this runner,
over the internal Compose network, with exactly three fields: the run id, the
run kind, and the run's MCP token. The runner then starts the team's agent CLI
as a child process with only what that run may have:

- a private directory, mode 0700, holding the MCP config, mode 0600, with the
  server URL and the token as an ``Authorization`` header. The token is never
  in argv or the environment, where tools and CLIs can print it.
- an environment allowlist: a few base variables, the model-login variables
  the team names, and one GitHub token chosen by run kind. Drafting and
  question runs get the read-only token, and only execution runs get the
  write token. A missing token refuses the run; there is no fallback.
- a time limit, after which the whole process group is killed

What to start comes from a named preset (#22), ``JOTFLOW_AGENT_PRESET``, or
from the team's own ``JOTFLOW_AGENT_COMMAND``. See :mod:`jotflow.adapters.presets`.

The runner authenticates JotFlow with a shared secret, compared in constant
time and never passed to the agent. It refuses any ``Origin``, any ``Host`` off
its allowlist, query strings, and large bodies, like the MCP gate does. The
agent's own output is discarded, since results come only through MCP tools (D1)
and the output may quote Slack text.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from jotflow.adapters import github_proof
from jotflow.adapters import isolation as isolation_mod
from jotflow.adapters.presets import (
    DEFAULT_GITHUB_MCP,
    GITHUB_TOKEN_CHARS,
    MODEL_NAME,
    PRESETS,
    CommandPreset,
    Preset,
    RunContext,
)
from jotflow.models import RunKind
from jotflow.netcheck import AddressRefused, Resolver, SystemResolver, checked_address
from jotflow.store.base import DEFAULT_RUN_TIMEOUTS
from jotflow.whiteboard.tokens import MAX_TOKEN_LENGTH, MAX_TOKEN_TTL_SECONDS, TOKEN_PREFIX

log = logging.getLogger("jotflow.runner")

RUNNER_PORT = 8766
MAX_BODY_BYTES = 4 * 1024
MAX_LIVE_RUNS = 8
# How long after SIGTERM a run's process group has before SIGKILL.
KILL_GRACE_SECONDS = 5.0
# A little longer than JotFlow's own limit, which also asks the runner to stop.
TIMEOUT_GRACE_SECONDS = 60
MCP_ALLOW_PUBLIC_ENV = "JOTFLOW_MCP_ALLOW_PUBLIC"
DIR_PREFIX = "jotflow-run-"

# Set by the runner for every run; a preset's extra env never replaces them.
RUNNER_SET_ENV = frozenset({"HOME", "TMPDIR"})
# Always passed through, when set; nothing else from the runner's own environment.
BASE_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "NODE_OPTIONS", "SSL_CERT_FILE")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Never handed to an agent, even if a team lists them.
_FORBIDDEN_ENV = re.compile(r"^JOTFLOW_|^SLACK_|_TOKEN$|^GH_|^GITHUB_")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOKEN_BODY = re.compile(r"[A-Za-z0-9_-]+")


CONVERSATION_KINDS = frozenset({RunKind.DRAFTING, RunKind.QUESTION})
EXECUTION_KINDS = frozenset({RunKind.EXECUTION})


class RunState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    KILLED = "killed"
    FAILED = "failed"


class LaunchRequest(BaseModel):
    """What JotFlow sends to start a run: never a command."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=rf"^{_RUN_ID.pattern}$")  # anchored: Pydantic searches
    kind: RunKind
    token: str = Field(min_length=len(TOKEN_PREFIX) + 1, max_length=MAX_TOKEN_LENGTH)

    @field_validator("token")
    @classmethod
    def _run_token(cls, value: str) -> str:
        # URL-safe characters only, as issued: nothing an agent CLI's ${VAR}
        # expansion in the MCP config could act on.
        if not value.startswith(TOKEN_PREFIX) or not _TOKEN_BODY.fullmatch(
            value[len(TOKEN_PREFIX) :]
        ):
            raise ValueError("token is not a JotFlow run token")
        return value


class ConfigError(Exception):
    """The runner's own configuration is unusable. Raised at startup."""


@dataclass(frozen=True)
class RunnerConfig:
    """What the runner needs, from its own environment, never JotFlow's.

    A runner serves either conversation runs (drafting and question), holding
    only the read-only GitHub token, or execution runs, holding only the write
    token. Same-uid processes in one container can read each other's
    ``/proc/<pid>/environ`` and run directories, so the write token must never
    exist in a container a drafting run can reach. Within one kind that is
    accepted: those runs hold the same token anyway.
    """

    secret: str = field(repr=False)
    mcp_url: str
    # Exactly one of these: the team's own argv template, or a named preset.
    command: tuple[str, ...] = ()
    preset: Preset | None = None
    kinds: frozenset[RunKind] = CONVERSATION_KINDS
    read_token: str | None = field(default=None, repr=False)
    write_token: str | None = field(default=None, repr=False)
    env_allow: tuple[str, ...] = ()
    allowed_hosts: frozenset[str] = frozenset()
    max_live: int = MAX_LIVE_RUNS
    workdir: Path | None = None
    # The model per run kind; a kind left out gets the preset's default.
    models: Mapping[RunKind, str] = field(default_factory=dict)
    github_mcp: str = DEFAULT_GITHUB_MCP
    # Hand the agent an MCP URL whose host resolves to a public address (#234). Off:
    # the run token goes only to a private, loopback or link-local address.
    mcp_allow_public: bool = False
    # Per-run uids (#181): "none", the default, or "uid", which needs the runner to
    # start as root with only SETUID and SETGID (docker-compose.isolation.yml).
    isolation: isolation_mod.Mode = "none"

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ConfigError("the runner secret must be at least 32 characters")
        if bool(self.command) == (self.preset is not None):
            raise ConfigError("set exactly one of JOTFLOW_AGENT_PRESET and JOTFLOW_AGENT_COMMAND")
        if not self.mcp_url.startswith(("http://", "https://")):
            raise ConfigError("the MCP URL must be http or https")
        if self.kinds not in (CONVERSATION_KINDS, EXECUTION_KINDS):
            raise ConfigError(
                "a runner serves either drafting and question runs, or execution runs only"
            )
        # A token left blank is named first, so the split rule below never hides it.
        if self.serves_execution and not self.write_token:
            raise ConfigError("JOTFLOW_GITHUB_WRITE_TOKEN is missing in agent-exec.env")
        if not self.serves_execution and not self.read_token:
            raise ConfigError("JOTFLOW_GITHUB_READ_TOKEN is missing in agent.env")
        if self.serves_execution:
            if self.write_token is None or self.read_token is not None:
                raise ConfigError(
                    "an execution runner holds the write GitHub token and never the read one"
                )
        elif self.read_token is None or self.write_token is not None:
            raise ConfigError(
                "a drafting and question runner holds the read GitHub token and never the write one"
            )
        for name in self.env_allow:
            if not _ENV_NAME.fullmatch(name):
                raise ConfigError(f"{name!r} is not an environment variable name")
            if _FORBIDDEN_ENV.search(name):
                raise ConfigError(
                    f"{name} may not be passed to the agent: JotFlow, Slack, and GitHub "
                    "variables are set by the runner itself"
                )
        self._check_preset()

    def _check_preset(self) -> None:
        allowed = getattr(self.preset, "run_kinds", None)
        if allowed is not None and not self.kinds <= allowed:
            names = " and ".join(sorted(k.value for k in self.kinds - allowed))
            raise ConfigError(
                f"the {self.agent.name} preset may not serve {names} runs; "
                "use claude-code in agent-exec.env for execution"
            )
        if self.preset is None and self.models:
            raise ConfigError(
                "JOTFLOW_AGENT_MODEL settings need a preset; put the model in "
                "JOTFLOW_AGENT_COMMAND instead"
            )
        for kind, model in self.models.items():
            if kind not in self.kinds:
                raise ConfigError(
                    f"this runner does not serve {kind.value} runs, so has no model for them"
                )
            if not MODEL_NAME.fullmatch(model):
                raise ConfigError(f"{model!r} is not a model name")
        verified = getattr(self.preset, "verified_models", None)
        if verified is not None:
            for kind, model in self.models.items():
                if model not in verified:
                    raise ConfigError(
                        f"the {self.agent.name} preset accepts only these models, whose tools "
                        f"were checked: {', '.join(verified)}; {kind.value} names {model!r}"
                    )
        if self.preset is not None and self.preset.github_token_in_config:
            for token in (self.read_token, self.write_token):
                if token is not None and not GITHUB_TOKEN_CHARS.fullmatch(token):
                    raise ConfigError("the GitHub token holds characters a GitHub token never has")

    @property
    def serves_execution(self) -> bool:
        return self.kinds == EXECUTION_KINDS

    @property
    def live_limit(self) -> int:
        """Execution runs go one at a time; conversation runs up to ``max_live``."""
        return 1 if self.serves_execution else self.max_live

    @property
    def agent(self) -> Preset:
        return self.preset if self.preset is not None else CommandPreset(self.command)

    def model(self, kind: RunKind) -> str | None:
        return self.models.get(kind) or self.agent.default_models.get(kind)

    def github_token(self, kind: RunKind) -> str | None:
        """The one GitHub token a run of ``kind`` gets. No fallback between them."""
        if kind not in self.kinds:
            return None
        return self.write_token if kind is RunKind.EXECUTION else self.read_token


def parse_command(raw: str) -> tuple[str, ...]:
    """``JOTFLOW_AGENT_COMMAND``: a JSON list of strings, run with no shell."""
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError("JOTFLOW_AGENT_COMMAND must be a JSON list of strings") from None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ConfigError("JOTFLOW_AGENT_COMMAND must be a JSON list of strings")
    return tuple(value)


def parse_preset(raw: str) -> Preset:
    """``JOTFLOW_AGENT_PRESET``: one of :data:`PRESETS`, by name."""
    preset = PRESETS.get(raw.strip())
    if preset is None:
        names = ", ".join(sorted(PRESETS))
        raise ConfigError(f"JOTFLOW_AGENT_PRESET must be one of: {names}")
    return preset


def child_env(
    config: RunnerConfig,
    kind: RunKind,
    run_id: str,
    run_dir: Path,
    environ: Mapping[str, str],
    *,
    extra: Mapping[str, str] | None = None,
    github_token_env: bool = True,
    home: Path | None = None,
) -> dict[str, str]:
    """The agent's whole environment. Only what is named here, from ``environ``,
    plus a preset's ``extra``, which never sets a base or allowlisted name, nor
    any name the runner sets itself."""
    passed = (*BASE_ENV, *config.env_allow)
    env = {
        k: v
        for k, v in (extra or {}).items()
        if k not in passed and k not in RUNNER_SET_ENV and not _FORBIDDEN_ENV.search(k)
    }
    env.update({name: environ[name] for name in passed if name in environ})
    token = config.github_token(kind)
    if token is None:  # pragma: no cover - launch refuses this first
        raise ConfigError(f"no GitHub token for {kind.value} runs")
    if github_token_env:
        env.update({"GITHUB_TOKEN": token, "GH_TOKEN": token})
    env.update(
        {
            # HOME stays the agent user's own, where the CLI keeps its model login;
            # only scratch files go to the run's private directory. With per-run uids
            # (#181), the run gets its own home and scratch space there too.
            "HOME": str(home) if home is not None else environ.get("HOME", str(run_dir)),
            "TMPDIR": str(run_dir / "tmp") if home is not None else str(run_dir),
            "JOTFLOW_RUN_ID": run_id,
            "JOTFLOW_RUN_KIND": kind.value,
            "JOTFLOW_MCP_CONFIG": str(run_dir / "mcp.json"),
        }
    )
    return env


def write_private(run_dir: Path, relative: str, text: str) -> Path:
    """Write ``text`` to ``run_dir/relative``: the file 0600, any new directory 0700.

    ``relative`` must stay inside the run directory, so a preset cannot write
    anywhere else.
    """
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or PurePosixPath(relative).is_absolute()
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ConfigError(
            f"a preset file must be a plain path inside the run directory: {relative!r}"
        )
    target = run_dir.joinpath(*parts)
    folder = run_dir
    for part in parts[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise ConfigError(f"a preset file's directory is a symlink: {relative!r}")
        if not folder.exists():
            folder.mkdir(mode=0o700)
            folder.chmod(0o700)  # whatever the umask
    # O_EXCL already refuses an existing file; O_NOFOLLOW refuses a symlink there too.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target


def time_limit(kind: RunKind) -> float:
    seconds = DEFAULT_RUN_TIMEOUTS[kind].total_seconds() + TIMEOUT_GRACE_SECONDS
    return float(min(seconds, MAX_TOKEN_TTL_SECONDS))


def remove_stale(workdir: Path | None) -> int:
    """Remove run directories a previous runner left behind. Returns how many."""
    root = workdir or Path(tempfile.gettempdir())
    removed = 0
    for entry in root.glob(f"{DIR_PREFIX}*"):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


@dataclass
class _Run:
    run_id: str
    kind: RunKind
    state: RunState = RunState.STARTING
    exit_code: int | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # The run's own uid, with JOTFLOW_RUNNER_ISOLATION=uid (#181).
    user: isolation_mod.RunUser | None = None

    def view(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "state": self.state.value, "exit_code": self.exit_code}


LIVE = frozenset({RunState.STARTING, RunState.RUNNING})


class Runner:
    """Starts, watches, and stops agent runs. One per agent container."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        environ: Mapping[str, str] | None = None,
        limit: Callable[[RunKind], float] = time_limit,
        kill_grace: float = KILL_GRACE_SECONDS,
        resolver: Resolver | None = None,
    ) -> None:
        self._config = config
        self._resolver: Resolver = resolver or SystemResolver()
        self._environ = dict(os.environ if environ is None else environ)
        self._limit = limit
        self._kill_grace = kill_grace
        self._runs: dict[str, _Run] = {}
        # Per-run uids (#181): the pool, when the runner is set to use one.
        self._pool = isolation_mod.UidPool() if config.isolation == "uid" else None

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        return None if run is None else run.view()

    def live(self) -> int:
        return sum(1 for run in self._runs.values() if run.state in LIVE)

    async def launch(self, request: LaunchRequest) -> dict[str, Any]:
        """Start a run. Raises :class:`LaunchRefused` if it may not start."""
        # Refusals that depend only on the config come first, so the reason is
        # the most specific one, such as a kind this runner doesn't serve.
        if request.kind not in self._config.kinds:
            served = " and ".join(sorted(k.value for k in self._config.kinds))
            raise LaunchRefused(403, f"this runner serves only {served} runs")
        if self._config.github_token(request.kind) is None:  # pragma: no cover - config refuses
            raise LaunchRefused(503, f"no GitHub token is configured for {request.kind.value} runs")
        # The only await comes next, before the checks on live state: nothing may
        # suspend between those and registering the run, or two launches of one
        # run could both pass them.
        try:
            await self.check_mcp_host()
        except AddressRefused as refused:
            # The agent would send its run token to that host (#234).
            log.warning("run %s refused: %s", request.run_id, refused)
            raise LaunchRefused(502, str(refused)) from None
        if request.run_id in self._runs:
            raise LaunchRefused(409, f"run {request.run_id} was already launched")
        if self.live() >= self._config.live_limit:
            raise LaunchRefused(429, "the runner is at its limit of live runs")
        user = None
        if self._pool is not None:
            user = self._pool.acquire()
            if user is None:
                raise LaunchRefused(429, "the runner has no free run uid")
        run = _Run(request.run_id, request.kind, user=user)
        self._runs[request.run_id] = run
        # Keep finished runs from growing without bound.
        for old in [r for r in self._runs.values() if r.state not in LIVE][
            : -self._config.max_live * 4
        ]:
            del self._runs[old.run_id]
        run.task = asyncio.create_task(self._run(run, request.token))
        log.info("run %s launching (%s)", run.run_id, run.kind.value)
        return run.view()

    async def check_mcp_host(self) -> str:
        """The address JotFlow's MCP host resolves to, private unless allowed (#234).

        The agent CLI does its own lookup afterwards. ``dns_search: ["."]`` in the
        Compose files stops that lookup falling back to a search domain.
        """
        url = urlsplit(self._config.mcp_url)
        port = url.port or (443 if url.scheme == "https" else 80)
        return await checked_address(
            self._resolver,
            url.hostname or "",
            port,
            allow_public=self._config.mcp_allow_public,
            opt_in=MCP_ALLOW_PUBLIC_ENV,
        )

    async def stop(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        run.stop.set()
        if run.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await run.task
        return run.view()

    async def close(self) -> None:
        """Stop every live run, for the runner's own shutdown."""
        await asyncio.gather(
            *(self.stop(r.run_id) for r in list(self._runs.values()) if r.state in LIVE)
        )

    async def _run(self, run: _Run, token: str) -> None:
        run_dir = Path(tempfile.mkdtemp(prefix=DIR_PREFIX, dir=self._config.workdir))
        user = run.user
        home: Path | None = None
        try:
            if user is not None:
                # The run's own home and scratch space, inside its directory (#181).
                home = run_dir / "home"
                home.mkdir(mode=0o700)
                (run_dir / "tmp").mkdir(mode=0o700)
            config_path = run_dir / "mcp.json"
            github_token = self._config.github_token(run.kind)
            if github_token is None:  # pragma: no cover - launch refuses this first
                raise ConfigError(f"no GitHub token for {run.kind.value} runs")
            plan = self._config.agent.plan(
                RunContext(
                    kind=run.kind,
                    run_id=run.run_id,
                    run_dir=run_dir,
                    config_path=config_path,
                    mcp_url=self._config.mcp_url,
                    mcp_token=token,
                    github_token=github_token,
                    model=self._config.model(run.kind),
                    github_mcp=self._config.github_mcp,
                )
            )
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(plan.config, handle)
            for relative, text in plan.files.items():
                write_private(run_dir, relative, text)
            if user is not None:
                isolation_mod.share_tree(run_dir, user)
            env = child_env(
                self._config,
                run.kind,
                run.run_id,
                run_dir,
                self._environ,
                extra=plan.env,
                github_token_env=plan.github_token_env,
                home=home,
            )
            options = {} if user is None else isolation_mod.spawn_options(user)
            try:
                run.process = await asyncio.create_subprocess_exec(
                    *plan.argv,
                    cwd=run_dir,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    **options,  # type: ignore[arg-type]
                )
            except OSError as exc:
                run.state = RunState.FAILED
                log.error("run %s could not start the agent (%s)", run.run_id, type(exc).__name__)
                return
            if user is not None and not isolation_mod.check_child(run.process.pid, user):
                # Fail closed: the drop didn't take (#181).
                log.error("run %s didn't drop to its own uid with no capabilities", run.run_id)
                run.state = RunState.FAILED
                await isolation_mod.kill_all(user)
                with contextlib.suppress(ProcessLookupError):
                    run.process.kill()
                await run.process.wait()
                return
            run.state = RunState.RUNNING
            await self._watch(run)
        finally:
            clean = True
            if user is not None:
                clean = await isolation_mod.sweep(user, run_dir)
            shutil.rmtree(run_dir, ignore_errors=True)
            if user is not None and self._pool is not None:
                self._pool.release(user, clean=clean and not run_dir.exists())
            log.info("run %s ended (%s)", run.run_id, run.state.value)

    async def _watch(self, run: _Run) -> None:
        process = run.process
        if process is None:  # pragma: no cover - _run sets it before watching
            return
        waiting = asyncio.create_task(process.wait())
        stopping = asyncio.create_task(run.stop.wait())
        done, _ = await asyncio.wait(
            {waiting, stopping}, timeout=self._limit(run.kind), return_when=asyncio.FIRST_COMPLETED
        )
        stopping.cancel()
        if waiting in done:
            run.exit_code = waiting.result()
            run.state = RunState.EXITED
            return
        run.state = RunState.KILLED if stopping in done else RunState.TIMED_OUT
        if run.user is not None:
            # The runner can't signal another uid: a helper as the run's uid does (#181).
            await isolation_mod.kill_all(run.user)
        else:
            await self._kill(process)
        run.exit_code = await waiting

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, sig)
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), self._kill_grace)
                return
            except TimeoutError:
                continue


class LaunchRefused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class RunnerGate:
    """The runner's front door: the same strictness as the MCP gate, plus the secret."""

    def __init__(self, app: ASGIApp, *, secret: str, allowed_hosts: Collection[str]) -> None:
        self._app = app
        self._secret = secret.encode()
        self._hosts = frozenset(h.lower() for h in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        problem = self._refusal(scope)
        if problem is not None:
            status, reason = problem
            await JSONResponse({"error": reason}, status_code=status)(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _refusal(self, scope: Scope) -> tuple[int, str] | None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if "origin" in headers:
            return 403, "requests with an Origin header are refused"
        if headers.get("host", "").lower() not in self._hosts:
            return 403, "Host is not allowed"
        if scope.get("query_string"):
            return 400, "query strings are not accepted"
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return 413, "request body is too large"
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.encode(), self._secret):
            return 401, "the runner secret is missing or wrong"
        return None


def runner_info(config: RunnerConfig) -> dict[str, Any]:
    """``GET /info``: the preset name (``command`` for a custom command) and kinds served."""
    return {
        "preset": config.agent.name,
        "kinds": [kind.value for kind in RunKind if kind in config.kinds],
    }


class VerifyRequest(BaseModel):
    """What JotFlow sends to check a GitHub link's one-time code (#195)."""

    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=39)
    code_digest: str = Field(min_length=64, max_length=64)


def build_runner_app(
    runner: Runner,
    config: RunnerConfig,
    *,
    github_transport: Any = None,
) -> ASGIApp:
    """The runner's HTTP app: GET /info, POST /runs, GET /runs/{id}, DELETE /runs/{id},
    and, on the drafting runner, POST /verify (#195)."""

    async def launch(request: Request) -> Response:
        # Read with a cap, so a chunked body without a length cannot grow unbounded.
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse({"error": "request body is too large"}, status_code=413)
        try:
            launch_request = LaunchRequest.model_validate_json(body)
        except ValidationError:
            return JSONResponse(
                {"error": "expected exactly run_id, kind, and a JotFlow run token"},
                status_code=400,
            )
        try:
            view = await runner.launch(launch_request)
        except LaunchRefused as refused:
            return JSONResponse({"error": refused.reason}, status_code=refused.status)
        return JSONResponse(view, status_code=202)

    async def status(request: Request) -> Response:
        view = runner.get(request.path_params["run_id"])
        if view is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        return JSONResponse(view)

    async def info(_: Request) -> Response:
        # What this runner starts, so JotFlow can check it matches jotflow.toml.
        # Behind the same gate as /runs: the secret is required.
        return JSONResponse(runner_info(config))

    async def stop(request: Request) -> Response:
        view = await runner.stop(request.path_params["run_id"])
        if view is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        return JSONResponse(view)

    async def verify(request: Request) -> Response:
        # Only the drafting runner checks codes: the execution runner, which holds
        # the write token, does nothing but execution runs.
        if config.serves_execution:
            return JSONResponse({"error": "this runner doesn't check codes"}, status_code=403)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse({"error": "request body is too large"}, status_code=413)
        try:
            asked = VerifyRequest.model_validate_json(body)
            async with github_proof.new_client(github_transport) as http:
                proof = await github_proof.check_proof(http, asked.login, asked.code_digest)
        except (ValidationError, ValueError):
            return JSONResponse(
                {"error": "expected exactly a GitHub login and a code digest"}, status_code=400
            )
        except github_proof.ProofError as failed:
            log.warning("a GitHub link check failed: %s", failed)
            return JSONResponse({"error": str(failed)}, status_code=502)
        log.info("a GitHub link check finished (verified: %s)", proof.verified)
        return JSONResponse(proof.model_dump())

    app = Starlette(
        routes=[
            Route("/info", info, methods=["GET"]),
            Route("/verify", verify, methods=["POST"]),
            Route("/runs", launch, methods=["POST"]),
            Route("/runs/{run_id}", status, methods=["GET"]),
            Route("/runs/{run_id}", stop, methods=["DELETE"]),
        ]
    )
    return RunnerGate(app, secret=config.secret, allowed_hosts=config.allowed_hosts)
