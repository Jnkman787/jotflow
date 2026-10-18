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
_NOT_COLUMNS = {"PRIMARY", "CHECK", "UNIQUE", "FOREIGN", "CONSTRAINT"}


def columns(sql: str) -> list[tuple[str, str, str]]:
    """``(name, type, comment)`` for each column in a stored CREATE TABLE statement.

    Splits the column list on top-level commas. A ``/* */`` comment belongs to
    the column it sits in. A ``--`` comment belongs to the column that starts its
    line, since the schema writes one column per line. That rule still holds after
    ``ALTER TABLE ... ADD COLUMN``, which SQLite splices in after the last column,
    even mid-line.
    """
    body = sql[sql.index("(") + 1 : sql.rindex(")")]
    # Each part: [definition, comment, line it starts on]
    parts: list[list[object]] = [["", "", None]]
    depth, i, line = 0, 0, 0
    while i < len(body):
        part = parts[-1]
        if body.startswith("--", i):
            end = body.find("\n", i)
            end = len(body) if end == -1 else end
            owners = [p for p in parts if p[2] == line and str(p[0]).strip()]
            owner = owners[0] if owners else parts[-2] if len(parts) > 1 else part
            owner[1] = f"{owner[1]} {body[i + 2 : end].strip()}"
            i = end
        elif body.startswith("/*", i):
            end = body.index("*/", i)
            part[1] = f"{part[1]} {body[i + 2 : end].strip()}"
            i = end + 2
        elif body[i] == "'":
            end = body.index("'", i + 1)
            part[0] = f"{part[0]}{body[i : end + 1]}"
            i = end + 1
        else:
            ch = body[i]
            line += ch == "\n"
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                parts.append(["", "", None])
            else:
                if part[2] is None and not ch.isspace():
                    part[2] = line
                part[0] = f"{part[0]}{ch}"
            i += 1
    found = []
    for definition, comment, _ in parts:
        words = str(definition).split()
        if len(words) >= 2 and words[0].upper() not in _NOT_COLUMNS:
            found.append((words[0], words[1], str(comment).strip()))
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
            "runs",
            "channel_modes",
            "tool_calls",
            "packet_messages",
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


async def test_the_check_reads_added_columns() -> None:
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE t (\n    id TEXT PRIMARY KEY -- an id\n)")
        db.execute("ALTER TABLE t ADD COLUMN cleared_at TEXT /* when cleared */")
        db.execute("ALTER TABLE t ADD COLUMN note TEXT /* the message's text */")
        db.execute("ALTER TABLE t ADD COLUMN bare TEXT")
        found = problems(db)
        assert "t.note: documented as holding a message" in found
        assert "t.bare: no comment saying what it holds" in found
        assert not any(p.startswith("t.cleared_at") for p in found)


def test_fresh_database_reaches_latest_version(db_path: Path) -> None:
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


def test_comments_stay_with_their_columns_after_add_column(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        (sql,) = db.execute("SELECT sql FROM sqlite_master WHERE name = 'proposals'").fetchone()
    found = {name: comment for name, _, comment in columns(sql)}
    assert found["updated_at"] == "UTC, ISO 8601"
    assert found["draft_cleared_at"].startswith("UTC, ISO 8601: when the drafted title")


def test_shipped_migrations_are_numbered_one_to_n() -> None:
    """Whoever rebases second renumbers; this catches a gap or a duplicate."""
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    assert len(MIGRATIONS) == LATEST_VERSION
