"""Tests close every direct SQLite connection (#263)."""

from __future__ import annotations

import gc
import sqlite3
import sys
import warnings
from pathlib import Path

import pytest

from tests.sqlite_db import opened


def test_opened_commits_then_closes(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with opened(path) as db:
        db.execute("CREATE TABLE t (x)")
        db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    with opened(path) as again:
        assert again.execute("SELECT x FROM t").fetchall() == [(1,)]


def test_opened_rolls_back_on_an_error_and_still_closes(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with opened(path) as db:
        db.execute("CREATE TABLE t (x)")
    with pytest.raises(RuntimeError), opened(path) as db:
        db.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("stop")
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")
    with opened(path) as again:
        assert again.execute("SELECT count(*) FROM t").fetchone() == (0,)


@pytest.mark.skipif(sys.version_info < (3, 13), reason="sqlite3 warns on a leak from 3.13")
def test_a_leaked_connection_is_caught(tmp_path: Path) -> None:
    """What pyproject's filterwarnings turns into a failure: ``with sqlite3.connect``
    alone never closes, so the connection is finalized later, with a warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with sqlite3.connect(tmp_path / "t.db") as db:
            db.execute("SELECT 1")
        del db
        gc.collect()
    assert any(issubclass(w.category, ResourceWarning) for w in caught)


def test_no_test_connects_without_closing() -> None:
    """Every direct connection in the tests goes through :func:`opened`."""
    here = Path(__file__).parent
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(here.rglob("*.py"))
        if path.name not in {"sqlite_db.py", Path(__file__).name}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "sqlite3.connect(" in line
    ]
    assert offenders == []
