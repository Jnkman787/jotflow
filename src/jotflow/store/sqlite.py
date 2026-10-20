"""The SQLite :class:`~jotflow.store.base.Store`, on aiosqlite.

One connection, used by one coroutine at a time behind an ``asyncio.Lock``.
Every write runs in ``BEGIN IMMEDIATE``, which takes SQLite's write lock up
front, so a check and the update that depends on it see the same row, even
when another process shares the file. Foreign keys are on, the journal is WAL,
and a busy timeout absorbs short waits for the lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

import aiosqlite
from pydantic import TypeAdapter

from jotflow.models import (
    ChannelMode,
    Duplicate,
    ExecutionResult,
    IssueProposal,
    MessageRef,
    Proposal,
    ProposalRecord,
    ProposalState,
    Run,
    RunKind,
    RunStatus,
    Topic,
    UpdateProposal,
)
from jotflow.store.base import (
    ACTIVE_RUN_STATUSES,
    DEFAULT_RUN_TIMEOUTS,
    DUPLICATE_MOVED_ON,
    DUPLICATE_NOT_NEW,
    DUPLICATE_TOPIC_LINKED,
    ChannelModeSetting,
    Clock,
    ConflictError,
    DuplicateRefusedError,
    EditField,
    IdentityLink,
    IdentityUnlink,
    LinkSource,
    MessageLink,
    NotFoundError,
    NoticeKind,
    OperatorToken,
    PendingThread,
    ProposalEdit,
    ReactionKeyword,
    ReactionMark,
    Recovery,
    StoreError,
    ToolCallRecord,
    TopicRecord,
    check_clearable,
    check_edit,
    check_finish,
    check_github_login,
    check_id,
    check_mark,
    check_new_proposal,
    check_notice,
    check_run_request,
    cleared_draft,
    edited_draft,
    from_text,
    plan_duplicate,
    plan_transition,
    plan_undo,
    to_text,
    utc_now,
)
from jotflow.store.hashing import canonical_json
from jotflow.store.lock import UNSUPPORTED, LockUnsupportedError, StoreLock, acquire
from jotflow.store.migrations import MIGRATIONS, Migration
from jotflow.store.titles import clean_title

log = logging.getLogger("jotflow.store")

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
        self._wal = False
        self._file_lock: StoreLock | None = None

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        clock: Clock = utc_now,
        migrations: Sequence[Migration] = MIGRATIONS,
        exclusive: bool = False,
        unlocked_ok: bool = False,
    ) -> Self:
        """Open or create the database at ``path`` and bring its schema up to date.

        A new file is created with mode 0600. With ``exclusive``, the store's
        lock is held until :meth:`close`, and :class:`StoreBusyError` is raised
        if another process holds it (:mod:`jotflow.store.lock`). Where the
        filesystem can't lock, ``unlocked_ok`` logs one warning and opens it
        unlocked; otherwise :class:`LockUnsupportedError` is raised.
        """
        file_lock = None
        if exclusive:
            try:
                file_lock = acquire(path)
            except LockUnsupportedError:
                if not unlocked_ok:
                    raise
                log.warning("%s", UNSUPPORTED)
        try:
            _create_private(path)
        except BaseException:
            if file_lock is not None:
                file_lock.release()
            raise
        db = await aiosqlite.connect(str(path), isolation_level=None)
        in_memory = str(path) == ":memory:"
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            # Overwrite deleted content, so a cleared draft leaves no copy in free pages.
            await db.execute("PRAGMA secure_delete = ON")
            await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not in_memory:
                await db.execute("PRAGMA journal_mode = WAL")
            await migrate(db, migrations)
        except BaseException:
            await db.close()
            if file_lock is not None:
                file_lock.release()
            raise
        store = cls(db, clock)
        store._wal = not in_memory
        store._file_lock = file_lock
        return store

    @property
    def locked(self) -> bool:
        """Whether this store holds the database's lock (:mod:`jotflow.store.lock`)."""
        return self._file_lock is not None

    async def close(self) -> None:
        try:
            await self._db.close()
        finally:
            if self._file_lock is not None:
                self._file_lock.release()

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
            card_ts=row["card_ts"],
            revision=row["revision"],
            card_revision=row["card_revision"],
            thread_told_at=None
            if row["thread_told_at"] is None
            else from_text(row["thread_told_at"]),
            draft_cleared_at=None
            if row["draft_cleared_at"] is None
            else from_text(row["draft_cleared_at"]),
            duplicate=_duplicate(row),
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

    async def proposal_for_card(self, channel_id: str, card_ts: str) -> ProposalRecord | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM proposals WHERE channel_id = ? AND card_ts = ?",
                (channel_id, card_ts),
            )
            return None if row is None else await self._proposal(row)

    async def run_proposals(self, run_id: str) -> list[ProposalRecord]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposals WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            )
            return [await self._proposal(row) for row in rows]

    async def thread_proposals(self, channel_id: str, thread_ts: str) -> list[ProposalRecord]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposals WHERE channel_id = ? AND thread_ts = ?"
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
            now = to_text(self._clock())
            cursor = await self._db.execute(
                """
                UPDATE proposals SET state = ?, content_hash = ?, approved_by = ?,
                    approved_at = ?, result_json = ?, updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = ?
                """,
                (
                    changes.state.value,
                    changes.content_hash,
                    changes.approved_by,
                    None if changes.approved_at is None else to_text(changes.approved_at),
                    None if changes.result is None else changes.result.model_dump_json(),
                    now,
                    proposal_id,
                    record.state.value,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - the write lock rules this out
                raise StoreError(f"proposal {proposal_id} changed during the transition")
            if changes.clear_draft:
                await self._clear(proposal_id, record.proposal, now)
            updated = await self._get_proposal(proposal_id)
        if changes.clear_draft:
            await self._scrub_wal()
        return _found(updated)

    async def supersede_pending(
        self, channel_id: str, thread_ts: str, *, keep_id: str
    ) -> list[str]:
        async with self._write():
            rows = await self._all(
                "SELECT id, draft_json FROM proposals WHERE channel_id = ? AND thread_ts = ?"
                " AND state = 'pending' AND id != ? ORDER BY id",
                (channel_id, thread_ts, keep_id),
            )
            now = to_text(self._clock())
            for row in rows:
                await self._db.execute(
                    "UPDATE proposals SET state = 'superseded', updated_at = ?,"
                    " revision = revision + 1"
                    " WHERE id = ? AND state = 'pending'",
                    (now, row["id"]),
                )
                await self._clear(row["id"], _PROPOSAL.validate_json(row["draft_json"]), now)
        if rows:
            await self._scrub_wal()
        return [row["id"] for row in rows]

    async def _clear(
        self, proposal_id: str, draft: IssueProposal | UpdateProposal, now: str
    ) -> None:
        """Replace the stored draft with its cleared form, inside the caller's transaction."""
        await self._db.execute(
            "UPDATE proposals SET draft_json = ?, draft_cleared_at = ?, updated_at = ?,"
            " revision = revision + 1"
            " WHERE id = ? AND draft_cleared_at IS NULL",
            (canonical_json(cleared_draft(draft)), now, now, proposal_id),
        )

    async def _scrub_wal(self) -> None:
        """Checkpoint and truncate the write-ahead log after a draft is cleared.

        secure_delete overwrites removed text in the main file, but in WAL mode
        the page as it was before the clear also sits in the -wal file until a
        checkpoint copies it back and the log is reset. A running JotFlow never
        closes its connection, so this is done explicitly. Clears are rare. If
        another connection is reading, the checkpoint cannot finish now; the next
        clear or SQLite's automatic checkpoint gets it.
        """
        if not self._wal:
            return
        async with self._lock:
            await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def edit_proposal(
        self,
        proposal_id: str,
        draft: IssueProposal | UpdateProposal,
        *,
        by: str,
        field: EditField,
    ) -> ProposalRecord:
        async with self._write():
            record = await self._get_proposal(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            check_edit(record, draft, by, field)
            draft = edited_draft(record, draft)
            now = to_text(self._clock())
            await self._db.execute(
                "UPDATE proposals SET draft_json = ?, repo = ?, updated_at = ?,"
                " revision = revision + 1 WHERE id = ? AND state = ?",
                (canonical_json(draft), draft.repo, now, proposal_id, record.state.value),
            )
            await self._db.execute(
                "INSERT INTO proposal_edits (proposal_id, edited_by, field, edited_at)"
                " VALUES (?, ?, ?, ?)",
                (proposal_id, by, field, now),
            )
            updated = await self._get_proposal(proposal_id)
        return _found(updated)

    async def proposal_edits(self, proposal_id: str) -> list[ProposalEdit]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposal_edits WHERE proposal_id = ? ORDER BY edited_at, rowid",
                (proposal_id,),
            )
        return [
            ProposalEdit(r["proposal_id"], r["edited_by"], r["field"], from_text(r["edited_at"]))
            for r in rows
        ]

    async def set_card_ts(self, proposal_id: str, ts: str) -> ProposalRecord:
        check_id(ts, "card ts")
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals SET card_ts = ?, updated_at = ? WHERE id = ?",
                (ts, to_text(self._clock()), proposal_id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"no proposal {proposal_id}")
            updated = await self._get_proposal(proposal_id)
        return _found(updated)

    async def cards_due(self, limit: int = 50) -> list[ProposalRecord]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposals AS p"
                " WHERE (p.card_revision IS NULL OR p.card_revision < p.revision)"
                " AND NOT EXISTS (SELECT 1 FROM channel_modes AS m"
                " WHERE m.channel_id = p.channel_id AND m.mode = 'paused')"
                # Auto mode's card waits for its write (#28), so it is not due until then.
                " AND NOT (p.card_ts IS NULL AND p.approved_by = 'auto'"
                " AND p.state IN ('approved', 'executing', 'reconciling'))"
                " ORDER BY p.created_at, p.id LIMIT ?",
                (limit,),
            )
            return [await self._proposal(row) for row in rows]

    async def mark_card_shown(self, proposal_id: str, revision: int) -> None:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals"
                " SET card_revision = MAX(COALESCE(card_revision, 0), MIN(?, revision))"
                " WHERE id = ?",
                (revision, proposal_id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"no proposal {proposal_id}")

    async def threads_due(self, limit: int = 50) -> list[ProposalRecord]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposals AS p"
                " WHERE p.state = 'applied' AND p.thread_told_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM channel_modes AS m"
                " WHERE m.channel_id = p.channel_id AND m.mode = 'paused')"
                " ORDER BY p.created_at, p.id LIMIT ?",
                (limit,),
            )
            return [await self._proposal(row) for row in rows]

    async def mark_thread_told(self, proposal_id: str) -> None:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals SET thread_told_at = COALESCE(thread_told_at, ?) WHERE id = ?",
                (to_text(self._clock()), proposal_id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"no proposal {proposal_id}")

    async def clear_draft(self, proposal_id: str) -> ProposalRecord:
        async with self._write():
            record = await self._get_proposal(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            check_clearable(record)
            cleared = record.draft_cleared_at is None
            await self._clear(proposal_id, record.proposal, to_text(self._clock()))
            updated = await self._get_proposal(proposal_id)
        if cleared:
            await self._scrub_wal()
        return _found(updated)

    async def mark_duplicate(
        self, proposal_id: str, *, index: int, expected_hash: str, by: str
    ) -> ProposalRecord:
        check_id(by, "Slack user id")
        async with self._write():
            record = await self._get_proposal(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            related = plan_duplicate(record, index, expected_hash)
            topic_id = record.proposal.topic_id
            topic = None if topic_id is None else await self._get_topic(topic_id)
            if topic is None or topic.topic.channel_id != record.channel_id:
                raise DuplicateRefusedError(DUPLICATE_NOT_NEW)
            if topic.topic.issue_number is not None:
                raise DuplicateRefusedError(DUPLICATE_TOPIC_LINKED)
            now = to_text(self._clock())
            await self._db.execute(
                "UPDATE proposals SET state = 'rejected', duplicate_repo = ?,"
                " duplicate_number = ?, duplicate_by = ?, duplicate_at = ?, updated_at = ?,"
                " revision = revision + 1 WHERE id = ? AND state = 'pending'",
                (related.repo, related.number, by, now, now, proposal_id),
            )
            await self._db.execute(
                "UPDATE topics SET repo = ?, issue_number = ?, last_activity_at = ? WHERE id = ?",
                (related.repo, related.number, now, topic.topic.id),
            )
            await self._clear(proposal_id, record.proposal, now)
            updated = await self._get_proposal(proposal_id)
        await self._scrub_wal()
        return _found(updated)

    async def undo_duplicate(self, proposal_id: str, *, by: str) -> ProposalRecord:
        check_id(by, "Slack user id")
        async with self._write():
            record = await self._get_proposal(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            duplicate = plan_undo(record)
            topic_id = record.proposal.topic_id
            topic = None if topic_id is None else await self._get_topic(topic_id)
            if topic is None or not _bound_to(topic.topic, duplicate):
                raise DuplicateRefusedError(DUPLICATE_MOVED_ON)
            written = await self._one(
                # Drafted after the "same as" and still live or written: it relies on
                # the binding, so unbinding would strand it or contradict its write.
                "SELECT 1 FROM proposals WHERE topic_id = ? AND created_at > ?"
                " AND state IN ('pending', 'approved', 'executing', 'reconciling', 'applied')",
                (topic.topic.id, to_text(duplicate.at)),
            )
            if written is not None:
                raise DuplicateRefusedError(DUPLICATE_MOVED_ON)
            now = to_text(self._clock())
            await self._db.execute(
                "UPDATE topics SET repo = NULL, issue_number = NULL WHERE id = ?",
                (topic.topic.id,),
            )
            await self._db.execute(
                "UPDATE proposals SET duplicate_undone_by = ?, duplicate_undone_at = ?,"
                " updated_at = ?, revision = revision + 1 WHERE id = ?",
                (by, now, now, proposal_id),
            )
            updated = await self._get_proposal(proposal_id)
        return _found(updated)

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

    async def remove_identity(self, slack_user_id: str, *, removed_by: str) -> bool:
        check_id(slack_user_id, "Slack user id")
        check_id(removed_by, "remover")
        async with self._write():
            cursor = await self._db.execute(
                "DELETE FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
            if cursor.rowcount == 0:
                return False
            await self._db.execute(
                "INSERT INTO identity_unlinks (id, slack_user_id, removed_by, removed_at)"
                " VALUES (?, ?, ?, ?)",
                (secrets.token_hex(8), slack_user_id, removed_by, to_text(self._clock())),
            )
            return True

    async def identity_unlinks(self) -> list[IdentityUnlink]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM identity_unlinks ORDER BY removed_at DESC, id DESC"
            )
        return [
            IdentityUnlink(
                row["id"], row["slack_user_id"], row["removed_by"], from_text(row["removed_at"])
            )
            for row in rows
        ]

    # Operator tokens

    @staticmethod
    def _operator(row: aiosqlite.Row) -> OperatorToken:
        return OperatorToken(
            id=row["id"],
            name=row["name"],
            digest=row["digest"],
            channels=tuple(json.loads(row["channels_json"])),
            created_at=from_text(row["created_at"]),
            expires_at=from_text(row["expires_at"]),
            revoked_at=from_text(row["revoked_at"]) if row["revoked_at"] else None,
            last_used_at=from_text(row["last_used_at"]) if row["last_used_at"] else None,
        )

    async def add_operator_token(self, token: OperatorToken) -> OperatorToken:
        async with self._write():
            try:
                await self._db.execute(
                    "INSERT INTO operator_tokens (id, name, digest, channels_json, created_at,"
                    " expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        token.id,
                        token.name,
                        token.digest,
                        json.dumps(list(token.channels)),
                        to_text(token.created_at),
                        to_text(token.expires_at),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(
                    f"an operator token named {token.name} already exists"
                ) from None
        return token

    async def operator_tokens(self) -> list[OperatorToken]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM operator_tokens ORDER BY created_at DESC, id DESC"
            )
        return [self._operator(row) for row in rows]

    async def revoke_operator_token(self, name: str) -> OperatorToken | None:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE operator_tokens SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (to_text(self._clock()), name),
            )
            if cursor.rowcount == 0:
                return None
            row = await self._one("SELECT * FROM operator_tokens WHERE name = ?", (name,))
        return None if row is None else self._operator(row)

    async def touch_operator_tokens(self, used: Mapping[str, datetime]) -> None:
        if not used:
            return
        async with self._write():
            for token_id, when in used.items():
                await self._db.execute(
                    "UPDATE operator_tokens SET last_used_at = ? WHERE id = ?"
                    " AND (last_used_at IS NULL OR last_used_at < ?)",
                    (to_text(when), token_id, to_text(when)),
                )

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

    async def add_reaction_mark(
        self, channel_id: str, ts: str, user_id: str, keyword: ReactionKeyword
    ) -> bool:
        check_mark(channel_id, ts, user_id, keyword)
        async with self._write():
            cursor = await self._db.execute(
                "INSERT OR IGNORE INTO reaction_marks (channel_id, ts, user_id, keyword, marked_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (channel_id, ts, user_id, keyword, to_text(self._clock())),
            )
            return cursor.rowcount == 1

    async def remove_reaction_mark(
        self, channel_id: str, ts: str, user_id: str, keyword: ReactionKeyword
    ) -> bool:
        check_mark(channel_id, ts, user_id, keyword)
        async with self._write():
            cursor = await self._db.execute(
                "DELETE FROM reaction_marks"
                " WHERE channel_id = ? AND ts = ? AND user_id = ? AND keyword = ?",
                (channel_id, ts, user_id, keyword),
            )
            return cursor.rowcount == 1

    async def reaction_marks(self, channel_id: str, ts: str) -> list[ReactionMark]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM reaction_marks WHERE channel_id = ? AND ts = ?"
                " ORDER BY marked_at, user_id, keyword",
                (channel_id, ts),
            )
        return [
            ReactionMark(
                r["channel_id"], r["ts"], r["user_id"], r["keyword"], from_text(r["marked_at"])
            )
            for r in rows
        ]

    async def unmark_chatter(self, ref: MessageRef) -> bool:
        async with self._write():
            cursor = await self._db.execute(
                "DELETE FROM message_refs WHERE channel_id = ? AND ts = ?"
                " AND chatter = 1 AND linked_by = 'person'",
                (ref.channel_id, ref.ts),
            )
            return cursor.rowcount == 1

    async def claim_notice(self, channel_id: str, user_id: str, kind: NoticeKind) -> bool:
        check_notice(channel_id, user_id, kind)
        async with self._write():
            cursor = await self._db.execute(
                "INSERT OR IGNORE INTO help_notices (channel_id, user_id, kind, sent_at)"
                " VALUES (?, ?, ?, ?)",
                (channel_id, user_id, kind, to_text(self._clock())),
            )
            return cursor.rowcount == 1

    async def release_notice(self, channel_id: str, user_id: str, kind: NoticeKind) -> None:
        check_notice(channel_id, user_id, kind)
        async with self._write():
            await self._db.execute(
                "DELETE FROM help_notices WHERE channel_id = ? AND user_id = ? AND kind = ?",
                (channel_id, user_id, kind),
            )

    async def get_channel_mode(self, channel_id: str) -> ChannelModeSetting | None:
        async with self._lock:
            row = await self._one("SELECT * FROM channel_modes WHERE channel_id = ?", (channel_id,))
        return None if row is None else self._mode(row)

    # Runs (D8, #53)

    @staticmethod
    def _run(row: aiosqlite.Row) -> Run:
        return Run(
            id=row["id"],
            kind=RunKind(row["kind"]),
            status=RunStatus(row["status"]),
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            proposal_id=row["proposal_id"],
            timeout_seconds=row["timeout_seconds"],
            created_at=from_text(row["created_at"]),
            deadline=from_text(row["deadline"]),
            started_at=None if row["started_at"] is None else from_text(row["started_at"]),
            finished_at=None if row["finished_at"] is None else from_text(row["finished_at"]),
            error=row["error"],
        )

    async def _get_run(self, run_id: str) -> Run | None:
        row = await self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return None if row is None else self._run(row)

    async def _active_run_for(
        self, kind: RunKind, channel_id: str, thread_ts: str, proposal_id: str | None
    ) -> Run | None:
        """The queued run new work of this kind would coalesce with, if any."""
        if kind is RunKind.DRAFTING:
            row = await self._one(
                "SELECT * FROM runs WHERE kind = 'drafting' AND status = 'queued'"
                " AND channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            )
        elif kind is RunKind.EXECUTION:
            row = await self._one(
                "SELECT * FROM runs WHERE kind = 'execution' AND status IN ('queued', 'running')"
                " AND proposal_id = ?",
                (proposal_id,),
            )
        else:
            return None
        return None if row is None else self._run(row)

    async def _insert_run(
        self,
        kind: RunKind,
        channel_id: str,
        thread_ts: str,
        proposal_id: str | None,
        seconds: int,
    ) -> str:
        run_id = secrets.token_hex(8)
        now = self._clock()
        await self._db.execute(
            """
            INSERT INTO runs (id, kind, status, channel_id, thread_ts, proposal_id,
                timeout_seconds, created_at, deadline)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind.value,
                channel_id,
                thread_ts,
                proposal_id,
                seconds,
                to_text(now),
                to_text(now + timedelta(seconds=seconds)),
            ),
        )
        return run_id

    async def enqueue_run(
        self,
        kind: RunKind,
        channel_id: str,
        thread_ts: str,
        *,
        proposal_id: str | None = None,
        timeout: timedelta | None = None,
    ) -> Run:
        seconds = check_run_request(
            kind,
            channel_id,
            thread_ts,
            proposal_id,
            DEFAULT_RUN_TIMEOUTS[kind] if timeout is None else timeout,
        )
        async with self._write():
            existing = await self._active_run_for(kind, channel_id, thread_ts, proposal_id)
            if existing is not None:
                return existing
            run_id = await self._insert_run(kind, channel_id, thread_ts, proposal_id, seconds)
            run = await self._get_run(run_id)
        return _found(run)

    # Ties in created_at break by rowid, which is insertion order, so runs
    # queued in the same instant still start first in, first out.

    async def claim_next_run(
        self, *, max_running: int, kinds: Collection[RunKind] | None = None
    ) -> Run | None:
        if max_running < 1:
            raise ValueError("max_running must be at least 1")
        async with self._write():
            row = await self._one("SELECT count(*) FROM runs WHERE status = 'running'")
            if row is not None and row[0] >= max_running:
                return None
            allowed = sorted(k.value for k in (kinds if kinds is not None else RunKind))
            if not allowed:
                return None
            marks = ", ".join("?" for _ in allowed)
            row = await self._one(
                f"""
                SELECT r.* FROM runs AS r
                WHERE r.status = 'queued' AND r.kind IN ({marks}) AND NOT EXISTS (
                    SELECT 1 FROM runs AS x WHERE x.status = 'running'
                    AND x.channel_id = r.channel_id AND x.thread_ts = r.thread_ts
                ) AND NOT EXISTS (
                    SELECT 1 FROM channel_modes AS m
                    WHERE m.channel_id = r.channel_id AND m.mode = 'paused'
                )
                ORDER BY r.created_at, r.rowid LIMIT 1
                """,
                tuple(allowed),
            )
            if row is None:
                return None
            now = self._clock()
            await self._db.execute(
                "UPDATE runs SET status = 'running', started_at = ?, deadline = ?"
                " WHERE id = ? AND status = 'queued'",
                (
                    to_text(now),
                    to_text(now + timedelta(seconds=row["timeout_seconds"])),
                    row["id"],
                ),
            )
            run = await self._get_run(row["id"])
        return _found(run)

    async def finish_run(
        self, run_id: str, status: RunStatus, *, error: str | None = None
    ) -> Run | None:
        async with self._write():
            run = await self._get_run(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            if run.status not in ACTIVE_RUN_STATUSES:
                return None
            bounded = check_finish(run, status, error)
            await self._db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?"
                " WHERE id = ? AND status = ?",
                (status.value, to_text(self._clock()), bounded, run_id, run.status.value),
            )
            finished = await self._get_run(run_id)
        return _found(finished)

    async def get_run(self, run_id: str) -> Run | None:
        async with self._lock:
            return await self._get_run(run_id)

    async def arm_thread(self, channel_id: str, thread_ts: str, due_at: datetime) -> None:
        check_id(channel_id, "channel id")
        check_id(thread_ts, "thread ts")
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        async with self._write():
            await self._db.execute(
                "INSERT INTO pending_threads (channel_id, thread_ts, due_at) VALUES (?, ?, ?)"
                " ON CONFLICT (channel_id, thread_ts) DO UPDATE SET due_at = excluded.due_at",
                (channel_id, thread_ts, to_text(due_at)),
            )

    async def pending_threads(self) -> list[PendingThread]:
        rows = await self._all(
            "SELECT channel_id, thread_ts, due_at FROM pending_threads"
            " ORDER BY due_at, channel_id, thread_ts"
        )
        return [
            PendingThread(row["channel_id"], row["thread_ts"], from_text(row["due_at"]))
            for row in rows
        ]

    async def queue_pending(self, channel_id: str, thread_ts: str) -> Run | None:
        seconds = check_run_request(
            RunKind.DRAFTING, channel_id, thread_ts, None, DEFAULT_RUN_TIMEOUTS[RunKind.DRAFTING]
        )
        async with self._write():
            cursor = await self._db.execute(
                "DELETE FROM pending_threads WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            )
            if cursor.rowcount == 0:
                return None
            skipped = await self._one(
                "SELECT 1 FROM message_refs WHERE channel_id = ? AND ts = ?"
                " AND chatter = 1 AND linked_by = 'person'",
                (channel_id, thread_ts),
            )
            if skipped is not None:
                return None
            existing = await self._active_run_for(RunKind.DRAFTING, channel_id, thread_ts, None)
            if existing is not None:
                return existing
            run_id = await self._insert_run(RunKind.DRAFTING, channel_id, thread_ts, None, seconds)
            run = await self._get_run(run_id)
        return _found(run)

    async def drop_pending(self, channel_id: str, thread_ts: str) -> None:
        async with self._write():
            await self._db.execute(
                "DELETE FROM pending_threads WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            )

    async def thread_runs(self, channel_id: str, thread_ts: str) -> list[Run]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM runs WHERE channel_id = ? AND thread_ts = ?"
                " ORDER BY created_at, rowid",
                (channel_id, thread_ts),
            )
        return [self._run(row) for row in rows]

    async def overdue_runs(self) -> list[Run]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM runs WHERE status = 'running' AND deadline < ?"
                " ORDER BY deadline, id",
                (to_text(self._clock()),),
            )
        return [self._run(row) for row in rows]

    async def recover_runs(self) -> Recovery:
        async with self._write():
            rows = await self._all(
                "SELECT * FROM runs WHERE status IN ('queued', 'running')"
                " ORDER BY created_at, rowid"
            )
            runs = [self._run(row) for row in rows]
            now = self._clock()
            await self._db.executemany(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE id = ?",
                [(to_text(now), run.id) for run in runs],
            )
            requeued: list[str] = []
            threads: set[tuple[str, str]] = set()
            timeouts: dict[str, int] = {}
            for run in runs:
                if run.kind is RunKind.DRAFTING and (run.channel_id, run.thread_ts) not in threads:
                    threads.add((run.channel_id, run.thread_ts))
                    requeued.append(
                        await self._insert_run(
                            run.kind, run.channel_id, run.thread_ts, None, run.timeout_seconds
                        )
                    )
                elif run.kind is RunKind.EXECUTION and run.proposal_id is not None:
                    timeouts.setdefault(run.proposal_id, run.timeout_seconds)
            # Every proposal waiting to be written gets one execution run, whether
            # or not its last run was recorded: this also covers a crash between
            # approving a proposal and queuing its run.
            waiting = await self._all(
                "SELECT id FROM proposals WHERE state IN ('approved', 'executing', 'reconciling')"
                " ORDER BY created_at, rowid"
            )
            reconciling: list[str] = []
            for row in waiting:
                record = _found(await self._get_proposal(row["id"]))
                if record.state is ProposalState.EXECUTING:
                    changes = plan_transition(
                        record,
                        ProposalState.RECONCILING,
                        expected={ProposalState.EXECUTING},
                        approved_by=None,
                        expected_hash=None,
                        result=None,
                        now=now,
                    )
                    await self._db.execute(
                        "UPDATE proposals SET state = ?, updated_at = ?, revision = revision + 1"
                        " WHERE id = ? AND state = 'executing'",
                        (changes.state.value, to_text(now), record.id),
                    )
                    reconciling.append(record.id)
                seconds = timeouts.get(
                    record.id, int(DEFAULT_RUN_TIMEOUTS[RunKind.EXECUTION].total_seconds())
                )
                requeued.append(
                    await self._insert_run(
                        RunKind.EXECUTION, record.channel_id, record.thread_ts, record.id, seconds
                    )
                )
        return Recovery(
            interrupted=tuple(run.id for run in runs),
            requeued=tuple(requeued),
            reconciling=tuple(reconciling),
        )

    # Write-tool ledger and packet membership

    async def find_tool_call(self, run_id: str, tool: str, args_hash: str) -> str | None:
        async with self._lock:
            row = await self._one(
                "SELECT result_json FROM tool_calls"
                " WHERE run_id = ? AND tool = ? AND args_hash = ?",
                (run_id, tool, args_hash),
            )
        return None if row is None else str(row["result_json"])

    async def record_tool_call(self, call: ToolCallRecord) -> int:
        check_id(call.run_id, "run id")
        check_id(call.channel_id, "channel id")
        called_at = to_text(call.called_at)
        async with self._write():
            taken = await self._one(
                "SELECT 1 FROM tool_calls WHERE run_id = ? AND tool = ? AND args_hash = ?",
                (call.run_id, call.tool, call.args_hash),
            )
            if taken is not None:
                raise ConflictError(f"run {call.run_id} already recorded this {call.tool} call")
            row = await self._one(
                "SELECT coalesce(max(seq), 0) + 1 AS next FROM tool_calls WHERE run_id = ?",
                (call.run_id,),
            )
            seq = int(row["next"]) if row is not None else 1
            await self._db.execute(
                """
                INSERT INTO tool_calls
                    (run_id, seq, tool, args_hash, channel_id, thread_ts, result_json, called_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call.run_id,
                    seq,
                    call.tool,
                    call.args_hash,
                    call.channel_id,
                    call.thread_ts,
                    call.result_json,
                    called_at,
                ),
            )
        return seq

    async def count_tool_calls(self, run_id: str, tools: Collection[str]) -> int:
        names = list(tools)
        if not names:
            return 0
        marks = ", ".join("?" for _ in names)
        async with self._lock:
            row = await self._one(
                f"SELECT count(*) AS n FROM tool_calls WHERE run_id = ? AND tool IN ({marks})",
                (run_id, *names),
            )
        return int(row["n"]) if row is not None else 0

    async def count_thread_calls(
        self, channel_id: str, thread_ts: str | None, tool: str, *, since: datetime
    ) -> int:
        async with self._lock:
            row = await self._one(
                "SELECT count(*) AS n FROM tool_calls WHERE channel_id = ? AND thread_ts IS ?"
                " AND tool = ? AND called_at >= ?",
                (channel_id, thread_ts, tool, to_text(since)),
            )
        return int(row["n"]) if row is not None else 0

    async def record_packet(self, run_id: str, refs: Collection[MessageRef]) -> None:
        check_id(run_id, "run id")
        rows = []
        for ref in refs:
            check_id(ref.channel_id, "channel id")
            check_id(ref.ts, "message timestamp")
            rows.append((run_id, ref.channel_id, ref.ts, ref.thread_ts))
        async with self._write():
            await self._db.executemany(
                "INSERT OR IGNORE INTO packet_messages (run_id, channel_id, ts, thread_ts)"
                " VALUES (?, ?, ?, ?)",
                rows,
            )

    async def packet_ref(self, run_id: str, channel_id: str, ts: str) -> MessageRef | None:
        async with self._lock:
            row = await self._one(
                "SELECT channel_id, ts, thread_ts FROM packet_messages"
                " WHERE run_id = ? AND channel_id = ? AND ts = ?",
                (run_id, channel_id, ts),
            )
        if row is None:
            return None
        return MessageRef(channel_id=row["channel_id"], ts=row["ts"], thread_ts=row["thread_ts"])


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


def _duplicate(row: aiosqlite.Row) -> Duplicate | None:
    if row["duplicate_repo"] is None:
        return None
    return Duplicate(
        repo=row["duplicate_repo"],
        number=row["duplicate_number"],
        by=row["duplicate_by"],
        at=from_text(row["duplicate_at"]),
        undone_by=row["duplicate_undone_by"],
        undone_at=None
        if row["duplicate_undone_at"] is None
        else from_text(row["duplicate_undone_at"]),
    )


def _bound_to(topic: Topic, duplicate: Duplicate) -> bool:
    return (
        topic.repo is not None
        and topic.repo.casefold() == duplicate.repo.casefold()
        and topic.issue_number == duplicate.number
    )
