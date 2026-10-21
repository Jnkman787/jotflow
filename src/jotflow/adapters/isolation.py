"""Per-run Linux uids inside an agent container (#181).

They're opt-in, with ``JOTFLOW_RUNNER_ISOLATION=uid``.

By default (``none``), every run in an agent service shares the runner's uid,
as before. With ``uid``, each run's agent runs as its own unprivileged uid from
a pool, so it can't read another run's environment, run directory or MCP
config, or the runner's own environment, which holds the GitHub token and the
runner secret.

It needs the runner to start as uid 0 with only ``SETUID`` and ``SETGID``, which
``docker-compose.isolation.yml`` adds. ``no-new-privileges`` and Docker's default
seccomp stay on. The runner checks that at startup and refuses otherwise:

- **Groups, not chown.** The runner joins every run group with ``setgroups``. A
  run's directory is ``0770 root:runN`` and its config ``0640``, so only that
  run's uid, the only member of ``runN``, can read them.
- **The drop.** The agent starts as the run's uid with no supplementary groups.
  Moving from uid 0 to all-nonzero ids clears every capability, which the runner
  checks in ``/proc/<pid>/status`` right after the start.
- **Stopping without CAP_KILL.** The runner can't signal another uid, so a short
  helper running as the run's uid calls ``kill(-1, SIGKILL)``. That ends every
  process the uid owns, a daemonized one included.
- **Sweeping before reuse.** The same helper empties the run directory, which
  the runner can't, and removes the uid's files in ``/dev/shm``. A uid that
  didn't sweep clean isn't handed out again.
- **API keys only.** Each run gets its own home, so a CLI login kept in the
  service's home can't be used. The preset's API key must be in the
  environment, or the runner refuses to start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("jotflow.runner")

Mode = Literal["none", "uid"]
MODES: tuple[Mode, ...] = ("none", "uid")
POOL_FIRST = 20001
POOL_SIZE = 64
# CAP_SETGID is bit 6 and CAP_SETUID bit 7: exactly these, and nothing else.
EXPECTED_CAPS = (1 << 6) | (1 << 7)
OVERRIDE = "docker-compose.isolation.yml"
HELPER_TIMEOUT_SECONDS = 10.0
# The model API key each preset reads, any one of which is enough.
API_KEYS: Mapping[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_API_KEY",),
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY"),
    "copilot": ("COPILOT_PROVIDER_API_KEY",),
}


class IsolationError(Exception):
    """Per-run uids can't work here. The message says what to change."""


@dataclass(frozen=True)
class RunUser:
    uid: int
    gid: int


def capabilities(status: str) -> tuple[list[int], int]:
    """The real, effective, saved and filesystem uids, and CapEff, from a status file."""
    uids: list[int] = []
    cap_eff = -1
    for line in status.splitlines():
        if line.startswith("Uid:"):
            uids = [int(x) for x in line.split()[1:]]
        elif line.startswith("CapEff:"):
            cap_eff = int(line.split()[1], 16)
    return uids, cap_eff


def check_startup(
    *,
    preset: str,
    env_allow: Collection[str],
    environ: Mapping[str, str],
    status: str,
) -> None:
    """Refuse unless the runner can drop runs to pool uids, and the login is an API key."""
    uids, cap_eff = capabilities(status)
    if not uids or uids[0] != 0 or cap_eff != EXPECTED_CAPS:
        raise IsolationError(
            "JOTFLOW_RUNNER_ISOLATION=uid needs the runner to start as root with only the "
            f"SETUID and SETGID capabilities: add {OVERRIDE} to COMPOSE_FILE, or set "
            "JOTFLOW_RUNNER_ISOLATION=none"
        )
    keys = API_KEYS.get(preset)
    if keys is not None and not any(
        name in env_allow and environ.get(name, "").strip() for name in keys
    ):
        raise IsolationError(
            "JOTFLOW_RUNNER_ISOLATION=uid gives each run its own home, so a CLI login in "
            f"the agent's home can't be used: put {' or '.join(keys)} in the agent's env "
            "file and in JOTFLOW_AGENT_ENV_ALLOW"
        )


# Runs as the run's uid. kill(-1) signals every process that uid owns, and none
# of the runner's: the uid owns nothing else. It loops until no process is left.
_KILL = """
import errno, os, signal
for _ in range(50):
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        break
"""

# Runs as the run's uid, after _KILL: empties the run directory, which the
# runner can't once the run made subdirectories, and the uid's files in /dev/shm.
_SWEEP = (
    _KILL
    + """
import shutil, sys
run_dir = sys.argv[1]
problems = 0
for name in os.listdir(run_dir):
    path = os.path.join(run_dir, name)
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except OSError:
        problems += 1
uid = os.getuid()
for root in ("/dev/shm",):
    try:
        names = os.listdir(root)
    except OSError:
        continue
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.lstat(path).st_uid == uid:
                shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
        except OSError:
            problems += 1
sys.exit(1 if problems else 0)
"""
)


class UidPool:
    """Hands out pool uids round-robin, never one that's live or wasn't swept clean."""

    def __init__(self, first: int = POOL_FIRST, size: int = POOL_SIZE) -> None:
        self._uids = list(range(first, first + size))
        self._next = 0
        self._busy: set[int] = set()

    @property
    def gids(self) -> list[int]:
        return list(self._uids)

    def acquire(self) -> RunUser | None:
        for _ in range(len(self._uids)):
            uid = self._uids[self._next]
            self._next = (self._next + 1) % len(self._uids)
            if uid not in self._busy:
                self._busy.add(uid)
                return RunUser(uid, uid)
        return None

    def release(self, user: RunUser, *, clean: bool) -> None:
        """Free ``user`` for reuse, or keep it out if its sweep found something."""
        if clean:
            self._busy.discard(user.uid)
        else:
            log.warning("uid %d didn't sweep clean, so it won't be reused", user.uid)


def join_run_groups(pool: UidPool) -> None:
    """Join every run group, so the runner can give a run its directory without chown."""
    os.setgroups(pool.gids)


def share(path: Path, user: RunUser, mode: int) -> None:
    """Give ``path`` to the run's group with ``mode``. The runner stays its owner."""
    os.chown(path, -1, user.gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def share_tree(run_dir: Path, user: RunUser) -> None:
    """The run directory and everything the runner wrote in it, for the run's group only.

    Directories ``0770`` so the run can write its scratch files; files ``0640``.
    """
    share(run_dir, user, 0o770)
    for root, dirs, files in os.walk(run_dir):
        for name in dirs:
            share(Path(root) / name, user, 0o770)
        for name in files:
            share(Path(root) / name, user, 0o640)


def spawn_options(user: RunUser) -> dict[str, object]:
    """``subprocess`` options that start the agent as the run's uid, with no other groups."""
    return {"user": user.uid, "group": user.gid, "extra_groups": []}


def check_child(pid: int, user: RunUser, *, proc: Path = Path("/proc")) -> bool:
    """Whether the started agent really is the run's uid, with no capabilities."""
    try:
        status = (proc / str(pid) / "status").read_text()
    except OSError:
        return True  # it already exited; nothing is left running as the wrong uid
    uids, cap_eff = capabilities(status)
    return bool(uids) and all(u == user.uid for u in uids) and cap_eff == 0


async def _helper(user: RunUser, code: str, *args: str) -> bool:
    """Run ``code`` as the run's uid. True if it finished cleanly in time."""
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            code,
            *args,
            env={},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            user=user.uid,
            group=user.gid,
            extra_groups=[],
        )
    except OSError as exc:
        log.warning("the helper for uid %d didn't start (%s)", user.uid, type(exc).__name__)
        return False
    try:
        return await asyncio.wait_for(process.wait(), HELPER_TIMEOUT_SECONDS) == 0
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return False


async def kill_all(user: RunUser) -> bool:
    """End every process the run's uid owns."""
    return await _helper(user, _KILL)


async def sweep(user: RunUser, run_dir: Path) -> bool:
    """End the uid's processes, empty its run directory and its /dev/shm files."""
    return await _helper(user, _SWEEP, str(run_dir))


async def sweep_stale(workdir: Path | None, prefix: str = "jotflow-run-") -> int:
    """At startup, empty run directories a previous runner left, as each one's run uid.

    The runner can't remove what a run created inside its directory, so each is
    swept as the uid its group names, then the runner's own removal finishes it.
    """
    import tempfile

    root = workdir or Path(tempfile.gettempdir())
    pool = set(UidPool().gids)
    swept = 0
    for entry in root.glob(f"{prefix}*"):
        if not entry.is_dir() or entry.is_symlink():
            continue
        gid = entry.stat().st_gid
        if gid in pool:
            await sweep(RunUser(gid, gid), entry)
            swept += 1
    return swept
