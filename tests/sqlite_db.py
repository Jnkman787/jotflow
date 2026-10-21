"""A direct SQLite connection for tests that inspect or tamper with a store's file.

``with sqlite3.connect(path) as db`` commits or rolls back, but never closes the
connection: it lingers until garbage collection finalizes it, on whatever thread
runs then. That was the likely cause of a one-off segfault under load (#263).
:func:`opened` commits the same way and always closes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def opened(path: str | Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path)
    try:
        with db:  # commit on success, roll back on an error, as before
            yield db
    finally:
        db.close()
