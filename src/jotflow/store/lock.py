"""One writer at a time: ``jotflow run`` and ``jotflow rebuild`` each hold the store's lock.

The lock is an advisory ``flock`` on ``<database>.lock``, held for as long as the
process keeps the store open. The kernel drops it when the process exits, so a
crash never leaves it stuck. Two containers that share the data volume see the
same lock, which is the point: ``docker compose run ... jotflow rebuild`` refuses
while the ``jotflow`` service is running, and the reverse.

Other host commands, such as ``jotflow operator`` and ``jotflow identity``, run
alongside ``jotflow run`` on purpose and don't take it. Where ``fcntl`` doesn't
exist, as on Windows, there is no lock, and the docs' "stop JotFlow first" is
the only guard.
"""

from __future__ import annotations

import os
from pathlib import Path

from jotflow.store.base import StoreError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]


class StoreBusyError(StoreError):
    """Another JotFlow process holds the store's lock."""


class StoreLock:
    """A held lock. :meth:`release` is safe to call more than once."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        os.close(fd)  # closing the descriptor drops the flock


def lock_path(database_path: str | Path) -> Path:
    return Path(f"{database_path}.lock")


def acquire(database_path: str | Path) -> StoreLock | None:
    """Take the store's lock without waiting, or raise :class:`StoreBusyError`.

    ``None`` for an in-memory store, or where the platform has no ``flock``.
    """
    if str(database_path) == ":memory:" or fcntl is None:
        return None
    fd = os.open(lock_path(database_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise StoreBusyError(
            "another JotFlow process is using the database: stop it first, "
            "for example with docker compose stop jotflow"
        ) from None
    return StoreLock(fd)
