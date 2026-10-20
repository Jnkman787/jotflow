"""One writer at a time: ``jotflow run`` and ``jotflow rebuild`` each hold the store's lock.

The lock is an advisory ``flock`` on ``<database>.lock``, held for as long as the
process keeps the store open. The kernel drops it when the process exits, so a
crash never leaves it stuck. Two containers that share the data volume see the
same lock, which is the point: ``docker compose run ... jotflow rebuild`` refuses
while the ``jotflow`` service is running, and the reverse.

The lock protects the database's integrity against two writers, not any secret.
Some filesystems refuse ``flock`` outright, such as some network mounts and
Docker Desktop's file sharing. Then :func:`acquire` raises
:class:`LockUnsupportedError`. ``jotflow run`` warns once and carries on
unlocked, as it did before the lock existed. ``jotflow rebuild`` refuses unless
told ``--no-lock``.

Other host commands, such as ``jotflow operator`` and ``jotflow identity``, run
alongside ``jotflow run`` on purpose and don't take it. Where ``fcntl`` doesn't
exist, as on Windows, there is no lock.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from jotflow.store.base import StoreError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

UNSUPPORTED = (
    "the database's filesystem doesn't support locking; make sure only one JotFlow uses it"
)


class StoreBusyError(StoreError):
    """Another JotFlow process holds the store's lock."""


class LockUnsupportedError(StoreError):
    """The database's filesystem refuses ``flock``, so no lock can be held."""


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
    """Take the store's lock without waiting.

    ``None`` for an in-memory store, or where the platform has no ``flock``.
    Raises :class:`StoreBusyError` when another process holds it,
    :class:`LockUnsupportedError` when the filesystem refuses ``flock``, and
    :class:`StoreError` when the lock file is a symlink. The descriptor is
    closed on every failure.
    """
    if str(database_path) == ":memory:" or fcntl is None:
        return None
    path = lock_path(database_path)
    # O_NOFOLLOW: a planted symlink must not make JotFlow open or create its target.
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StoreError(f"the lock file is a symlink: remove {path}") from None
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise StoreBusyError(
            "another JotFlow process is using the database: stop it first, "
            "for example with docker compose stop jotflow"
        ) from None
    except OSError as error:
        os.close(fd)
        raise LockUnsupportedError(UNSUPPORTED) from error
    return StoreLock(fd)
