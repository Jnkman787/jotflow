"""The SQLite :class:`~jotflow.store.base.Store`, on aiosqlite.

One connection, used by one coroutine at a time behind an ``asyncio.Lock``.
Every write runs in ``BEGIN IMMEDIATE``, which takes SQLite's write lock up
front, so a check and the update that depends on it see the same row, even
when another process shares the file. Foreign keys are on, the journal is WAL,
and a busy timeout absorbs short waits for the lock.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import aiosqlite
from pydantic import TypeAdapter

from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    MessageRef,
    Proposal,
    ProposalRecord,
    ProposalState,
    Topic,
)
from jotflow.store.base import (
    ChannelModeSetting,
    Clock,
    ConflictError,
    IdentityLink,
    LinkSource,
    MessageLink,
    NotFoundError,
    StoreError,
    TopicRecord,
    check_github_login,
    check_id,
    check_new_proposal,
    from_text,
    plan_transition,
    to_text,
    utc_now,
)
from jotflow.store.hashing import canonical_json
from jotflow.store.migrations import MIGRATIONS, Migration
from jotflow.store.titles import clean_title

_PROPOSAL = TypeAdapter[Proposal](Proposal)
DB_FILE_MODE = 0o600
BUSY_TIMEOUT_MS = 5000


class SchemaVersionError(StoreError):
    """The database was written by a newer JotFlow."""


def _found[T](value: T | None) -> T:
    """A row just written must be readable; raise rather than assert, so -O keeps the check."""
    if value is None:  # pragma: no cover - would mean SQLite lost a committed row
        raise StoreError("a row written in this transaction could not be read back")
    return value


def _create_private(path: str | Path) -> None:
    """Create the database file readable only by its owner, before SQLite opens it.

    SQLite gives the -wal and -shm files the main file's permissions, so they
    follow. Outside Docker the process umask is often 022, which would leave
    drafts and identity links readable by other local users.
    """
    if str(path) == ":memory:" or str(path).startswith("file:"):
        return
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, DB_FILE_MODE)
    except FileExistsError:
        return
    os.close(fd)


class SqliteStore:
    def __init__(self, db: aiosqlite.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock
        self._lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        clock: Clock = utc_now,
        migrations: Sequence[Migration] = MIGRATIONS,
    ) -> Self:
        """Open or create the database at ``path`` and bring its schema up to date.

        A new file is created with mode 0600.
        """
        _create_private(path)
        db = await aiosqlite.connect(str(path), isolation_level=None)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if str(path) != ":memory:":
                await db.execute("PRAGMA journal_mode = WAL")
            await migrate(db, migrations)
        except BaseException:
            await db.close()
            raise
        return cls(db, clock)

    async def close(self) -> None:
        await self._db.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
            await self._db.execute("COMMIT")

    async def _one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._db.execute(sql, params) as cursor:
            row: aiosqlite.Row | None = await cursor.fetchone()
            return row

    async def _all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    # Slack message pointers

    @staticmethod
    def _link(row: aiosqlite.Row) -> MessageLink:
        ref = MessageRef(
            channel_id=row["channel_id"],
            ts=row["ts"],
            thread_ts=row["thread_ts"],
            permalink=row["permalink"],
        )
        return MessageLink(
            ref,
            row["topic_id"],
            bool(row["chatter"]),
            row["linked_by"],
            from_text(row["linked_at"]),
        )

    async def _decide(self, ref: MessageRef, topic_id: str | None, by: LinkSource) -> MessageLink:
        check_id(ref.channel_id, "channel id")
        check_id(ref.ts, "message timestamp")
        existing = await self._one(
            "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?", (ref.channel_id, ref.ts)
        )
        if existing is not None and existing["linked_by"] == "person" and by == "agent":
            return self._link(existing)
        now = to_text(self._clock())
        await self._db.execute(
            """
            INSERT INTO message_refs
                (channel_id, ts, thread_ts, permalink, topic_id, chatter, linked_by, linked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (channel_id, ts) DO UPDATE SET
                thread_ts = excluded.thread_ts,
                permalink = excluded.permalink,
                topic_id = excluded.topic_id,
                chatter = excluded.chatter,
                linked_by = excluded.linked_by,
                linked_at = excluded.linked_at
            """,
            (
                ref.channel_id,
                ref.ts,
                ref.thread_ts,
                ref.permalink,
                topic_id,
                int(topic_id is None),
                by,
                now,
            ),
        )
        if topic_id is not None:
            await self._db.execute(
                "UPDATE topics SET last_activity_at = ? WHERE id = ?", (now, topic_id)
            )
        row = await self._one(
            "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?", (ref.channel_id, ref.ts)
        )
        return self._link(_found(row))

    async def link_message(
        self, ref: MessageRef, topic_id: str, *, by: LinkSource = "agent"
    ) -> MessageLink:
        async with self._write():
            topic = await self._one("SELECT channel_id FROM topics WHERE id = ?", (topic_id,))
            if topic is None:
                raise NotFoundError(f"no topic {topic_id}")
            if topic["channel_id"] != ref.channel_id:
                raise ConflictError("a message can only join a topic in its own channel")
            return await self._decide(ref, topic_id, by)

    async def mark_chatter(self, ref: MessageRef, *, by: LinkSource = "agent") -> MessageLink:
        async with self._write():
            return await self._decide(ref, None, by)

    async def get_message(self, channel_id: str, ts: str) -> MessageLink | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?", (channel_id, ts)
            )
        return None if row is None else self._link(row)

    # Topics

    async def _topic(self, row: aiosqlite.Row) -> TopicRecord:
        messages = await self._all(
            "SELECT * FROM message_refs WHERE topic_id = ? ORDER BY ts", (row["id"],)
        )
        topic = Topic(
            id=row["id"],
            channel_id=row["channel_id"],
            title=row["title"],
            repo=row["repo"],
            issue_number=row["issue_number"],
            messages=[self._link(m).ref for m in messages],
        )
        return TopicRecord(topic, from_text(row["created_at"]), from_text(row["last_activity_at"]))

    async def _get_topic(self, topic_id: str) -> TopicRecord | None:
        row = await self._one("SELECT * FROM topics WHERE id = ?", (topic_id,))
        return None if row is None else await self._topic(row)

    async def create_topic(self, channel_id: str, title: str) -> TopicRecord:
        check_id(channel_id, "channel id")
        cleaned = clean_title(title)
        topic_id = secrets.token_hex(8)
        async with self._write():
            now = to_text(self._clock())
            await self._db.execute(
                "INSERT INTO topics (id, channel_id, title, created_at, last_activity_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (topic_id, channel_id, cleaned, now, now),
            )
            record = await self._get_topic(topic_id)
        return _found(record)

    async def get_topic(self, topic_id: str) -> TopicRecord | None:
        async with self._lock:
            return await self._get_topic(topic_id)

    async def _update_topic(self, topic_id: str, sql: str, params: Sequence[Any]) -> TopicRecord:
        async with self._write():
            cursor = await self._db.execute(sql, (*params, topic_id))
            if cursor.rowcount == 0:
                raise NotFoundError(f"no topic {topic_id}")
            record = await self._get_topic(topic_id)
        return _found(record)

    async def rename_topic(self, topic_id: str, title: str) -> TopicRecord:
        cleaned = clean_title(title)
        return await self._update_topic(
            topic_id, "UPDATE topics SET title = ? WHERE id = ?", (cleaned,)
        )

    async def set_topic_issue(self, topic_id: str, repo: str, issue_number: int) -> TopicRecord:
        if issue_number <= 0:
            raise ValueError("an issue number is positive")
        return await self._update_topic(
            topic_id,
            "UPDATE topics SET repo = ?, issue_number = ? WHERE id = ?",
            (repo, issue_number),
        )

    async def channel_topics(
        self, channel_id: str, *, active_since: datetime | None = None
    ) -> list[TopicRecord]:
        async with self._lock:
            if active_since is None:
                rows = await self._all(
                    "SELECT * FROM topics WHERE channel_id = ?"
                    " ORDER BY last_activity_at DESC, id DESC",
                    (channel_id,),
                )
            else:
                rows = await self._all(
                    "SELECT * FROM topics WHERE channel_id = ? AND last_activity_at >= ?"
                    " ORDER BY last_activity_at DESC, id DESC",
                    (channel_id, to_text(active_since)),
                )
            return [await self._topic(row) for row in rows]

    # Proposals

    async def _proposal(self, row: aiosqlite.Row) -> ProposalRecord:
        users = await self._all(
            "SELECT slack_user_id FROM proposal_participants WHERE proposal_id = ?"
            " ORDER BY slack_user_id",
            (row["id"],),
        )
        return ProposalRecord(
            id=row["id"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            run_id=row["run_id"],
            state=ProposalState(row["state"]),
            proposal=_PROPOSAL.validate_json(row["draft_json"]),
            content_hash=row["content_hash"],
            approved_by=row["approved_by"],
            approved_at=None if row["approved_at"] is None else from_text(row["approved_at"]),
            result=None
            if row["result_json"] is None
            else ExecutionResult.model_validate_json(row["result_json"]),
            participants=[u["slack_user_id"] for u in users],
            created_at=from_text(row["created_at"]),
        )

    async def _get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        row = await self._one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return None if row is None else await self._proposal(row)

    async def add_proposal(self, record: ProposalRecord) -> ProposalRecord:
        check_new_proposal(record)
        draft = record.proposal
        async with self._write():
            if await self._one("SELECT 1 FROM proposals WHERE id = ?", (record.id,)):
                raise ConflictError(f"proposal {record.id} already exists")
            if draft.topic_id is not None:
                topic = await self._one(
                    "SELECT channel_id FROM topics WHERE id = ?", (draft.topic_id,)
                )
                if topic is None:
                    raise NotFoundError(f"no topic {draft.topic_id}")
                if topic["channel_id"] != record.channel_id:
                    raise ConflictError("a proposal can only be for a topic in its own channel")
            now = to_text(self._clock())
            await self._db.execute(
                """
                INSERT INTO proposals (id, channel_id, thread_ts, run_id, state, kind, repo,
                    topic_id, draft_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.channel_id,
                    record.thread_ts,
                    record.run_id,
                    record.state.value,
                    draft.kind,
                    draft.repo,
                    draft.topic_id,
                    canonical_json(draft),
                    to_text(record.created_at),
                    now,
                ),
            )
            await self._db.executemany(
                "INSERT INTO proposal_participants (proposal_id, slack_user_id) VALUES (?, ?)",
                [(record.id, user) for user in sorted(set(record.participants))],
            )
            stored = await self._get_proposal(record.id)
        return _found(stored)

    async def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        async with self._lock:
            return await self._get_proposal(proposal_id)

    async def thread_proposals(
        self, channel_id: str, thread_ts: str | None
    ) -> list[ProposalRecord]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposals WHERE channel_id = ? AND thread_ts IS ?"
                " ORDER BY created_at, id",
                (channel_id, thread_ts),
            )
            return [await self._proposal(row) for row in rows]

    async def transition(
        self,
        proposal_id: str,
        to: ProposalState,
        *,
        expected: Collection[ProposalState] | None = None,
        approved_by: str | None = None,
        expected_hash: str | None = None,
        result: ExecutionResult | None = None,
    ) -> ProposalRecord:
        async with self._write():
            record = await self._get_proposal(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            changes = plan_transition(
                record,
                to,
                expected=expected,
                approved_by=approved_by,
                expected_hash=expected_hash,
                result=result,
                now=self._clock(),
            )
            cursor = await self._db.execute(
                """
                UPDATE proposals SET state = ?, content_hash = ?, approved_by = ?,
                    approved_at = ?, result_json = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    changes.state.value,
                    changes.content_hash,
                    changes.approved_by,
                    None if changes.approved_at is None else to_text(changes.approved_at),
                    None if changes.result is None else changes.result.model_dump_json(),
                    to_text(self._clock()),
                    proposal_id,
                    record.state.value,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - the write lock rules this out
                raise StoreError(f"proposal {proposal_id} changed during the transition")
            updated = await self._get_proposal(proposal_id)
        return _found(updated)

    async def supersede_pending(
        self, channel_id: str, thread_ts: str | None, *, keep_id: str
    ) -> list[str]:
        async with self._write():
            rows = await self._all(
                "SELECT id FROM proposals WHERE channel_id = ? AND thread_ts IS ?"
                " AND state = 'pending' AND id != ? ORDER BY id",
                (channel_id, thread_ts, keep_id),
            )
            ids = [row["id"] for row in rows]
            await self._db.executemany(
                "UPDATE proposals SET state = 'superseded', updated_at = ?"
                " WHERE id = ? AND state = 'pending'",
                [(to_text(self._clock()), proposal_id) for proposal_id in ids],
            )
            return ids

    # Identity links

    @staticmethod
    def _identity(row: aiosqlite.Row) -> IdentityLink:
        return IdentityLink(
            row["slack_user_id"],
            row["github_login"],
            bool(row["verified"]),
            from_text(row["linked_at"]),
        )

    async def link_identity(self, slack_user_id: str, github_login: str) -> IdentityLink:
        check_id(slack_user_id, "Slack user id")
        check_github_login(github_login)
        async with self._write():
            taken = await self._one(
                "SELECT slack_user_id FROM identity_links WHERE github_login = ?"
                " AND slack_user_id != ?",
                (github_login, slack_user_id),
            )
            if taken is not None:
                raise ConflictError(f"GitHub login {github_login} is already linked")
            await self._db.execute(
                """
                INSERT INTO identity_links (slack_user_id, github_login, verified, linked_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    github_login = excluded.github_login,
                    verified = 0,
                    linked_at = excluded.linked_at
                """,
                (slack_user_id, github_login, to_text(self._clock())),
            )
            row = await self._one(
                "SELECT * FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
        return self._identity(_found(row))

    async def get_identity(self, slack_user_id: str) -> IdentityLink | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
        return None if row is None else self._identity(row)

    async def unlink_identity(self, slack_user_id: str) -> bool:
        async with self._write():
            cursor = await self._db.execute(
                "DELETE FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
            return cursor.rowcount > 0

    # Channel modes

    @staticmethod
    def _mode(row: aiosqlite.Row) -> ChannelModeSetting:
        return ChannelModeSetting(
            row["channel_id"],
            ChannelMode(row["mode"]),
            row["changed_by"],
            from_text(row["changed_at"]),
        )

    async def set_channel_mode(
        self, channel_id: str, mode: ChannelMode, *, changed_by: str
    ) -> ChannelModeSetting:
        check_id(channel_id, "channel id")
        check_id(changed_by, "Slack user id")
        async with self._write():
            await self._db.execute(
                """
                INSERT INTO channel_modes (channel_id, mode, changed_by, changed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (channel_id) DO UPDATE SET
                    mode = excluded.mode,
                    changed_by = excluded.changed_by,
                    changed_at = excluded.changed_at
                """,
                (channel_id, ChannelMode(mode).value, changed_by, to_text(self._clock())),
            )
            row = await self._one("SELECT * FROM channel_modes WHERE channel_id = ?", (channel_id,))
        return self._mode(_found(row))

    async def get_channel_mode(self, channel_id: str) -> ChannelModeSetting | None:
        async with self._lock:
            row = await self._one("SELECT * FROM channel_modes WHERE channel_id = ?", (channel_id,))
        return None if row is None else self._mode(row)


async def schema_version(db: aiosqlite.Connection) -> int:
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def migrate(db: aiosqlite.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Apply pending migrations, one transaction each. Returns the version reached."""
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError("migrations must be numbered 1, 2, 3, ... with no gaps")
    current = await schema_version(db)
    if current > len(migrations):
        raise SchemaVersionError(
            f"the database is at schema version {current}, newer than this JotFlow "
            f"({len(migrations)}); upgrade JotFlow or restore a backup"
        )
    for migration in migrations[current:]:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                await db.execute(statement)
            await db.execute(f"PRAGMA user_version = {int(migration.version)}")
        except BaseException:
            await db.execute("ROLLBACK")
            raise
        await db.execute("COMMIT")
    return await schema_version(db)
