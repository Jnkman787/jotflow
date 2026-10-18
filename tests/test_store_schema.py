"""The schema never holds a Slack message body, and its migrations behave.

The schema test reads the live schema from ``sqlite_master``, so it also covers
tables later migrations add, such as run records (#53).
"""

from __future__ import annotations

import re
import sqlite3
import stat
from pathlib import Path

import pytest

from jotflow.store import SchemaVersionError, SqliteStore
from jotflow.store.migrations import LATEST_VERSION, MIGRATIONS, Migration
from jotflow.store.sqlite import migrate

# A column is suspect if its name is, or contains as a word, one of these.
_BODY_WORDS = {"body", "text", "content", "message", "msg", "words"}
# Derived values that only sound like content.
_ALLOWED_NAMES = {"content_hash"}
_DOCUMENTED_AS_MESSAGE = re.compile(
    r"\bmessage'?s?\s+(?:body|text|content|words)\b"
    r"|\b(?:body|text|content|words)\s+of\s+(?:a|the)\s+(?:slack\s+)?message\b"
    r"|\bwhat\s+(?:people|someone|they)\s+(?:wrote|said)\b",
    re.IGNORECASE,
)
_COLUMN = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+([A-Z]+)\b[^\n]*?(?:--\s*(.*))?$")
_NOT_COLUMNS = {"PRIMARY", "CHECK", "UNIQUE", "FOREIGN", "CONSTRAINT"}


def columns(sql: str) -> list[tuple[str, str, str]]:
    """``(name, type, comment)`` for each column in a CREATE TABLE statement."""
    found = []
    for line in sql.splitlines()[1:]:
        match = _COLUMN.match(line)
        if match and match.group(1).upper() not in _NOT_COLUMNS:
            found.append((match.group(1), match.group(2), (match.group(3) or "").strip()))
    return found


def problems(db: sqlite3.Connection) -> list[str]:
    """Every column that is undocumented, or named or documented as a message body."""
    found = []
    for table, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"):
        for name, _type, comment in columns(sql):
            words = set(name.split("_"))
            if name not in _ALLOWED_NAMES and words & _BODY_WORDS:
                found.append(f"{table}.{name}: named like a message body")
            if not comment:
                found.append(f"{table}.{name}: no comment saying what it holds")
            elif _DOCUMENTED_AS_MESSAGE.search(comment):
                found.append(f"{table}.{name}: documented as holding a message")
    return found


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "jotflow.db"
    store = await SqliteStore.open(path)
    await store.close()
    return path


def test_no_column_holds_a_message_body(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        assert problems(db) == []


def test_every_table_is_checked(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {
            "topics",
            "message_refs",
            "proposals",
            "proposal_participants",
            "identity_links",
            "channel_modes",
        }
        for table in tables:
            (sql,) = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
            real = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert {name for name, _, _ in columns(sql)} == real, table


@pytest.mark.parametrize(
    ("column", "complaint"),
    [
        ("message_text TEXT, -- what was posted", "named like a message body"),
        ("body TEXT, -- the reply", "named like a message body"),
        ("words TEXT, -- stuff", "named like a message body"),
        ("note TEXT, -- the message's text, for search", "documented as holding a message"),
        ("note TEXT, -- full text of the Slack message", "documented as holding a message"),
        ("note TEXT, -- what people wrote in the thread", "documented as holding a message"),
        ("note TEXT,", "no comment"),
    ],
)
def test_the_check_catches_a_message_column(column: str, complaint: str) -> None:
    with sqlite3.connect(":memory:") as db:
        sql = "\n".join(
            [
                "CREATE TABLE bad (",
                "    id TEXT PRIMARY KEY, -- an id",
                f"    {column}",
                "    x INTEGER -- x",
                ")",
            ]
        )
        db.execute(sql)
        found = problems(db)
        assert any(complaint in p for p in found), found


async def test_fresh_database_reaches_latest_version(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_reopening_is_a_no_op_and_keeps_data(db_path: Path) -> None:
    store = await SqliteStore.open(db_path)
    topic = await store.create_topic("C0IDEAS01", "Progress bar")
    await store.close()
    again = await SqliteStore.open(db_path)
    fetched = await again.get_topic(topic.topic.id)
    await again.close()
    assert fetched == topic


async def test_a_newer_database_is_refused(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    with pytest.raises(SchemaVersionError):
        await SqliteStore.open(db_path)


async def test_a_failing_migration_rolls_back(tmp_path: Path) -> None:
    broken = (
        *MIGRATIONS,
        Migration(
            LATEST_VERSION + 1,
            ("CREATE TABLE extra (id TEXT PRIMARY KEY -- an id\n)", "NOT VALID SQL"),
        ),
    )
    path = tmp_path / "jotflow.db"
    with pytest.raises(sqlite3.OperationalError):
        await SqliteStore.open(path, migrations=broken)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'extra'").fetchone() is None


async def test_migrations_must_be_numbered_without_gaps(tmp_path: Path) -> None:
    import aiosqlite

    async with aiosqlite.connect(tmp_path / "x.db", isolation_level=None) as db:
        with pytest.raises(ValueError):
            await migrate(db, (Migration(2, ("SELECT 1",)),))


async def test_in_memory_sqlite_works() -> None:
    store = await SqliteStore.open(":memory:")
    async with store:
        assert (await store.create_topic("C0IDEAS01", "Progress bar")).topic.title == "Progress bar"


async def test_a_new_database_is_private(tmp_path: Path) -> None:
    path = tmp_path / "jotflow.db"
    store = await SqliteStore.open(path)
    await store.create_topic("C0IDEAS01", "Progress bar")
    try:
        for name in ("jotflow.db", "jotflow.db-wal", "jotflow.db-shm"):
            mode = stat.S_IMODE((tmp_path / name).stat().st_mode)
            assert mode & 0o077 == 0, (name, oct(mode))
    finally:
        await store.close()


async def test_an_existing_database_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "jotflow.db"
    path.touch(mode=0o640)
    path.chmod(0o640)
    store = await SqliteStore.open(path)
    await store.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
