"""The SQLite :class:`~jotflow.store.base.Store`, on aiosqlite.

One connection, used by one coroutine at a time behind an ``asyncio.Lock``.
Every write runs in ``BEGIN IMMEDIATE``, which takes SQLite's write lock up
front, so a check and the update that depends on it see the same row, even
when another process shares the file. Foreign keys are on, the journal is WAL,
and a busy timeout absorbs short waits for the lock.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import secrets
import sqlite3
import typing
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

import aiosqlite
from pydantic import TypeAdapter

from jotflow.models import (
    AttachedFile,
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
    ALSO_NOT_LINKED,
    ALSO_PERSON,
    ALSO_TOO_MANY,
    DEFAULT_RUN_TIMEOUTS,
    DETACHED_RUN,
    DUPLICATE_MOVED_ON,
    DUPLICATE_NOT_NEW,
    DUPLICATE_TOPIC_LINKED,
    MAX_SUMMARY_ATTEMPTS,
    MAX_TOPICS_PER_MESSAGE,
    OPERATOR_REMOVER,
    SELF_ADD_RUN,
    SERVED_FILE,
    SUMMARY_POSTING_STALE,
    VERIFICATION_REMOVER,
    ChannelModeSetting,
    Clock,
    ConflictError,
    DuplicateRefusedError,
    EditField,
    IdentityCode,
    IdentityLink,
    IdentityUnlink,
    LinkSource,
    MessageLink,
    NotFoundError,
    NoticeKind,
    OperatorToken,
    PendingThread,
    ProposalEdit,
    ProposalRevision,
    ReactionKeyword,
    ReactionMark,
    Recovery,
    RevisedField,
    StaleRevisionError,
    StoreError,
    SummaryDeferral,
    SummaryPost,
    SummaryStatus,
    ToolCallRecord,
    TopicRecord,
    Verified,
    check_clearable,
    check_edit,
    check_finish,
    check_github_login,
    check_id,
    check_mark,
    check_new_proposal,
    check_notice,
    check_revision,
    check_run_request,
    check_sources_on_topic,
    cleared_draft,
    edited_draft,
    from_text,
    plan_duplicate,
    plan_transition,
    plan_undo,
    revised_draft,
    to_text,
    utc_now,
)
from jotflow.store.hashing import canonical_json
from jotflow.store.lock import UNSUPPORTED, LockUnsupportedError, StoreLock, acquire
from jotflow.store.migrations import MIGRATIONS, Migration
from jotflow.store.titles import clean_title

# Every field a revision row may name: a run's, and JotFlow's own "attached" (#391).
_CHANGED_FIELDS = frozenset(typing.get_args(RevisedField))
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
        # Whether the schema has message_topics (#335): a store opened at an older
        # migration, as upgrade tests do, has none, and so no message has extra topics.
        self._split = True

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
        found = await (
            await db.execute("SELECT 1 FROM sqlite_master WHERE name = 'message_topics'")
        ).fetchone()
        store._split = found is not None
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
    def _link(row: aiosqlite.Row, also: Sequence[str] = ()) -> MessageLink:
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
            tuple(also),
        )

    async def _also_of(self, channel_id: str, ts: str) -> list[str]:
        if not self._split:
            return []
        rows = await self._all(
            "SELECT topic_id FROM message_topics WHERE channel_id = ? AND ts = ?"
            " ORDER BY added_at, rowid",
            (channel_id, ts),
        )
        return [r["topic_id"] for r in rows]

    async def _linked(self, row: aiosqlite.Row) -> MessageLink:
        return self._link(row, await self._also_of(row["channel_id"], row["ts"]))

    async def _decide(self, ref: MessageRef, topic_id: str | None, by: LinkSource) -> MessageLink:
        check_id(ref.channel_id, "channel id")
        check_id(ref.ts, "message timestamp")
        existing = await self._one(
            "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?", (ref.channel_id, ref.ts)
        )
        if existing is not None and existing["linked_by"] == "person" and by == "agent":
            return await self._linked(existing)
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
        # A person's decision, or chatter, wins over the agent's split (#335); and the
        # first topic is never also an extra one.
        extra: list[str] = []
        if not self._split:
            pass
        elif by == "person" or topic_id is None:
            extra = await self._also_of(ref.channel_id, ref.ts)
            await self._db.execute(
                "DELETE FROM message_topics WHERE channel_id = ? AND ts = ?",
                (ref.channel_id, ref.ts),
            )
        else:
            await self._db.execute(
                "DELETE FROM message_topics WHERE channel_id = ? AND ts = ? AND topic_id = ?",
                (ref.channel_id, ref.ts, topic_id),
            )
        # Every topic the message leaves, a split's or not, whoever decided (#374, #395),
        # drops its pending drafts once it has no messages.
        first = existing["topic_id"] if existing is not None else None
        dropped: list[str] = []
        for other in dict.fromkeys([*extra, *([first] if first else [])]):
            if other != topic_id:
                dropped += await self._drop_orphaned(other, now)
                if by == "person" or topic_id is None:
                    await self._detach(other, ref.ts, now)
        row = await self._one(
            "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?", (ref.channel_id, ref.ts)
        )
        return dataclasses.replace(await self._linked(_found(row)), dropped=tuple(dropped))

    async def _detach(self, topic_id: str, ts: str, now: str) -> None:
        """Take a moved message's files off the topic's pending drafts (#391).

        A person moved message ``ts`` off ``topic_id``, or undid its split, so a file
        from it no longer belongs to that topic's draft. The draft keeps the rest, and
        a revision row names JotFlow, so the card says why and an earlier approval of
        the old text is stale. Approved or filed work stays as it is.
        """
        rows = await self._all(
            "SELECT id, run_id, draft_json FROM proposals WHERE topic_id = ?"
            " AND state = 'pending' AND draft_cleared_at IS NULL AND run_id != ?"
            " AND kind = 'create_issue' ORDER BY id",
            (topic_id, SELF_ADD_RUN),
        )
        for row in rows:
            draft = _PROPOSAL.validate_json(row["draft_json"])
            if not isinstance(draft, IssueProposal) or not draft.attached:
                continue
            kept = [a for a in draft.attached if await self._served_ts(row["run_id"], a) != ts]
            if len(kept) == len(draft.attached):
                continue
            detached = draft.model_copy(update={"attached": kept})
            await self._db.execute(
                "UPDATE proposals SET draft_json = ?, updated_at = ?, revision = revision + 1"
                " WHERE id = ?",
                (canonical_json(detached), now, row["id"]),
            )
            revision = await self._one("SELECT revision FROM proposals WHERE id = ?", (row["id"],))
            await self._db.execute(
                "INSERT INTO proposal_revisions (proposal_id, revision, run_id, changed,"
                " revised_at) VALUES (?, ?, ?, ?, ?)",
                (row["id"], _found(revision)["revision"], DETACHED_RUN, "attached", now),
            )

    async def _served_ts(self, run_id: str, attached: AttachedFile) -> str | None:
        """The message the run was served ``attached`` on, from the ledger."""
        row = await self._one(
            "SELECT result_json FROM tool_calls WHERE run_id = ? AND tool = ? AND args_hash = ?",
            (run_id, SERVED_FILE, attached.file_id),
        )
        if row is None:
            return None
        ts = json.loads(row["result_json"]).get("ts")
        return ts if isinstance(ts, str) else None

    async def _has_messages(self, topic_id: str) -> bool:
        """Whether any message is on the topic, as its first or an extra one."""
        if self._split:
            row = await self._one(
                "SELECT 1 FROM message_refs WHERE topic_id = ?"
                " UNION ALL SELECT 1 FROM message_topics WHERE topic_id = ? LIMIT 1",
                (topic_id, topic_id),
            )
        else:
            row = await self._one(
                "SELECT 1 FROM message_refs WHERE topic_id = ? LIMIT 1", (topic_id,)
            )
        return row is not None

    async def _drop_orphaned(self, topic_id: str, now: str) -> list[str]:
        """Supersede a topic's pending proposals once it has no messages left (#335).

        A decision that moves a topic's last message elsewhere, a person's or the
        agent's, split or not (#395), leaves it nothing to draft from. Its pending
        drafts are dropped, marked so the card says why; anything approved or filed,
        and an Add me request, stays as it is.
        """
        if await self._has_messages(topic_id):
            return []
        rows = await self._all(
            "SELECT id, draft_json FROM proposals WHERE topic_id = ? AND state = 'pending'"
            " AND run_id != ? ORDER BY id",
            (topic_id, SELF_ADD_RUN),
        )
        # unsplit_at came with message_topics, in migration 23: an older schema has
        # neither, and its card says only that a newer proposal replaced it.
        mark = "unsplit_at = ?, " if self._split else ""
        for row in rows:
            await self._db.execute(
                f"UPDATE proposals SET state = 'superseded', {mark}updated_at = ?,"
                " revision = revision + 1 WHERE id = ? AND state = 'pending'",
                (*([now] if self._split else []), now, row["id"]),
            )
            await self._clear(row["id"], _PROPOSAL.validate_json(row["draft_json"]), now)
        return [row["id"] for row in rows]

    async def _split_siblings(self, proposal_id: str) -> set[str]:
        """The topics that share a split message with this proposal's topic (#335).

        One pasted message can feed up to three topics, each with its own draft in
        the same thread, so a new draft for one never replaces another's.
        """
        if not self._split:
            return set()
        row = await self._one("SELECT topic_id FROM proposals WHERE id = ?", (proposal_id,))
        if row is None or row["topic_id"] is None:
            return set()
        topic_id = row["topic_id"]
        rows = await self._all(
            "SELECT r.topic_id AS first, m.topic_id AS extra FROM message_topics m"
            " JOIN message_refs r ON r.channel_id = m.channel_id AND r.ts = m.ts"
            " WHERE r.topic_id = ? OR EXISTS (SELECT 1 FROM message_topics k"
            " WHERE k.channel_id = m.channel_id AND k.ts = m.ts AND k.topic_id = ?)",
            (topic_id, topic_id),
        )
        siblings = {t for r in rows for t in (r["first"], r["extra"]) if t is not None}
        siblings.discard(topic_id)
        return siblings

    async def link_message(
        self, ref: MessageRef, topic_id: str, *, by: LinkSource = "agent"
    ) -> MessageLink:
        async with self._write():
            topic = await self._one("SELECT channel_id FROM topics WHERE id = ?", (topic_id,))
            if topic is None:
                raise NotFoundError(f"no topic {topic_id}")
            if topic["channel_id"] != ref.channel_id:
                raise ConflictError("a message can only join a topic in its own channel")
            link = await self._decide(ref, topic_id, by)
        if link.dropped:
            await self._scrub_wal()
        return link

    async def mark_chatter(self, ref: MessageRef, *, by: LinkSource = "agent") -> MessageLink:
        async with self._write():
            link = await self._decide(ref, None, by)
        if link.dropped:
            await self._scrub_wal()
        return link

    async def get_message(self, channel_id: str, ts: str) -> MessageLink | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?", (channel_id, ts)
            )
            return None if row is None else await self._linked(row)

    async def also_link(self, ref: MessageRef, topic_id: str) -> MessageLink:
        async with self._write():
            topic = await self._one("SELECT channel_id FROM topics WHERE id = ?", (topic_id,))
            if topic is None:
                raise NotFoundError(f"no topic {topic_id}")
            if topic["channel_id"] != ref.channel_id:
                raise ConflictError("a message can only join a topic in its own channel")
            row = await self._one(
                "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?",
                (ref.channel_id, ref.ts),
            )
            if row is None or row["topic_id"] is None:
                raise ConflictError(ALSO_NOT_LINKED)
            if row["linked_by"] == "person":
                raise ConflictError(ALSO_PERSON)
            also = await self._also_of(ref.channel_id, ref.ts)
            if topic_id == row["topic_id"] or topic_id in also:
                return self._link(row, also)
            if 1 + len(also) >= MAX_TOPICS_PER_MESSAGE:
                raise ConflictError(ALSO_TOO_MANY)
            now = to_text(self._clock())
            await self._db.execute(
                "INSERT INTO message_topics (channel_id, ts, topic_id, added_at)"
                " VALUES (?, ?, ?, ?)",
                (ref.channel_id, ref.ts, topic_id, now),
            )
            await self._db.execute(
                "UPDATE topics SET last_activity_at = ? WHERE id = ?", (now, topic_id)
            )
            return self._link(row, [*also, topic_id])

    # Topics

    async def _topic(self, row: aiosqlite.Row) -> TopicRecord:
        if self._split:
            messages = await self._all(
                "SELECT * FROM message_refs WHERE topic_id = ?"
                " UNION SELECT r.* FROM message_refs r JOIN message_topics m"
                " ON m.channel_id = r.channel_id AND m.ts = r.ts WHERE m.topic_id = ?"
                " ORDER BY ts",
                (row["id"], row["id"]),
            )
        else:
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
            card_issues=row["card_issues"],
            approved_issues=row["approved_issues"],
            skipped_label=row["skipped_label"],
            unsplit_at=from_text(row["unsplit_at"])
            if self._split and row["unsplit_at"] is not None
            else None,
        )

    async def _get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        row = await self._one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return None if row is None else await self._proposal(row)

    async def add_proposal(
        self, record: ProposalRecord, *, sources_on_topic: bool = False
    ) -> ProposalRecord:
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
                if sources_on_topic:
                    links = {}
                    for source in draft.sources:
                        row = await self._one(
                            "SELECT * FROM message_refs WHERE channel_id = ? AND ts = ?",
                            (record.channel_id, source.ts),
                        )
                        links[source.ts] = None if row is None else await self._linked(row)
                    check_sources_on_topic(draft.topic_id, draft.sources, links)
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
        issues: str | None = None,
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
                issues=issues,
            )
            now = to_text(self._clock())
            cursor = await self._db.execute(
                """
                UPDATE proposals SET state = ?, content_hash = ?, approved_by = ?,
                    approved_at = ?, result_json = ?, updated_at = ?, revision = revision + 1,
                    approved_issues = CASE WHEN ? THEN ? ELSE approved_issues END
                WHERE id = ? AND state = ?
                """,
                (
                    changes.state.value,
                    changes.content_hash,
                    changes.approved_by,
                    None if changes.approved_at is None else to_text(changes.approved_at),
                    None if changes.result is None else changes.result.model_dump_json(),
                    now,
                    changes.sets_issues,
                    changes.issues,
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
            spared = await self._split_siblings(keep_id)
            rows = [
                row
                for row in await self._all(
                    "SELECT id, topic_id, draft_json FROM proposals"
                    " WHERE channel_id = ? AND thread_ts = ? AND state = 'pending' AND id != ?"
                    " AND run_id != ? ORDER BY id",
                    (channel_id, thread_ts, keep_id, SELF_ADD_RUN),
                )
                if row["topic_id"] not in spared
            ]
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
        self,
        proposal_id: str,
        draft: IssueProposal | UpdateProposal,
        now: str,
        *,
        bump: bool = False,
    ) -> None:
        """Replace the stored draft with its cleared form, inside the caller's transaction.

        A clear that comes with a state change rides on that change's revision, as the
        memory store's does (#395). Only a clear on its own, ``bump``, raises it.
        """
        await self._db.execute(
            "UPDATE proposals SET draft_json = ?, draft_cleared_at = ?, updated_at = ?,"
            " revision = revision + ?"
            " WHERE id = ? AND draft_cleared_at IS NULL",
            (canonical_json(cleared_draft(draft)), now, now, int(bump), proposal_id),
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

    async def revise_proposal(
        self,
        proposal_id: str,
        draft: IssueProposal | UpdateProposal,
        *,
        run_id: str,
        base_revision: int,
        added_by: Collection[str],
    ) -> ProposalRecord:
        async with self._write():
            record = await self._get_proposal(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            counted = await self._one(
                "SELECT COUNT(*) AS n FROM proposal_revisions WHERE proposal_id = ?",
                (proposal_id,),
            )
            changed = check_revision(
                record,
                draft,
                base_revision=base_revision,
                added_by=added_by,
                revisions=int(counted["n"]) if counted else 0,
            )
            draft = revised_draft(record, draft)
            now = to_text(self._clock())
            cursor = await self._db.execute(
                "UPDATE proposals SET draft_json = ?, updated_at = ?, revision = revision + 1"
                " WHERE id = ? AND state = 'pending' AND revision = ?",
                (canonical_json(draft), now, proposal_id, base_revision),
            )
            if cursor.rowcount != 1:  # pragma: no cover - the write lock rules this out
                raise StaleRevisionError(record)
            revision = base_revision + 1
            await self._db.execute(
                "INSERT INTO proposal_revisions (proposal_id, revision, run_id, changed,"
                " revised_at) VALUES (?, ?, ?, ?, ?)",
                (proposal_id, revision, run_id, ",".join(changed), now),
            )
            for user in sorted(set(added_by)):
                await self._db.execute(
                    "INSERT INTO proposal_revision_authors (proposal_id, revision, user_id)"
                    " VALUES (?, ?, ?)",
                    (proposal_id, revision, user),
                )
                await self._db.execute(
                    "INSERT OR IGNORE INTO proposal_participants (proposal_id, slack_user_id)"
                    " VALUES (?, ?)",
                    (proposal_id, user),
                )
            updated = await self._get_proposal(proposal_id)
        return _found(updated)

    async def proposal_revisions(self, proposal_id: str) -> list[ProposalRevision]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM proposal_revisions WHERE proposal_id = ? ORDER BY revision",
                (proposal_id,),
            )
            authors = await self._all(
                "SELECT revision, user_id FROM proposal_revision_authors"
                " WHERE proposal_id = ? ORDER BY user_id",
                (proposal_id,),
            )
        by_revision: dict[int, list[str]] = {}
        for row in authors:
            by_revision.setdefault(int(row["revision"]), []).append(row["user_id"])
        return [
            ProposalRevision(
                r["proposal_id"],
                int(r["revision"]),
                r["run_id"],
                tuple(f for f in r["changed"].split(",") if f in _CHANGED_FIELDS),
                tuple(by_revision.get(int(r["revision"]), ())),
                from_text(r["revised_at"]),
            )
            for r in rows
        ]

    async def mark_follow_up_due(self, proposal_id: str) -> None:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals SET follow_up_due_at = COALESCE(follow_up_due_at, ?)"
                " WHERE id = ?",
                (to_text(self._clock()), proposal_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"no proposal {proposal_id}")

    async def take_follow_up_due(self, proposal_id: str) -> bool:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals SET follow_up_due_at = NULL"
                " WHERE id = ? AND follow_up_due_at IS NOT NULL",
                (proposal_id,),
            )
            return cursor.rowcount == 1

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

    async def mark_card_shown(
        self, proposal_id: str, revision: int, issues: str | None = None
    ) -> None:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals"
                " SET card_revision = MAX(COALESCE(card_revision, 0), MIN(?, revision)),"
                " card_issues = COALESCE(?, card_issues)"
                " WHERE id = ?",
                (revision, issues, proposal_id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"no proposal {proposal_id}")

    async def set_skipped_label(self, proposal_id: str, label: str) -> None:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE proposals SET skipped_label = ? WHERE id = ?", (label, proposal_id)
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
            await self._clear(proposal_id, record.proposal, to_text(self._clock()), bump=True)
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
            from_text(row["verified_at"]) if row["verified_at"] else None,
            row["github_id"],
            "admin" if row["linked_by"] == "admin" else "self",
        )

    async def count_self_adds(self, slack_user_id: str, since: datetime) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM proposals p JOIN proposal_participants pp"
            " ON pp.proposal_id = p.id WHERE p.run_id = ? AND pp.slack_user_id = ?"
            " AND p.created_at >= ?",
            (SELF_ADD_RUN, slack_user_id, to_text(since)),
        )
        return int(row["n"]) if row is not None else 0

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
                    verified_at = NULL,
                    github_id = NULL,
                    linked_at = excluded.linked_at,
                    linked_by = 'self'
                """,
                (slack_user_id, github_login, to_text(self._clock())),
            )
            # A changed link drops a pending code (#195).
            await self._db.execute(
                "DELETE FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
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
            await self._db.execute(
                "DELETE FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )
            cursor = await self._db.execute(
                "DELETE FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
            return cursor.rowcount > 0

    async def remove_identity(self, slack_user_id: str, *, removed_by: str) -> bool:
        check_id(slack_user_id, "Slack user id")
        check_id(removed_by, "remover")
        async with self._write():
            await self._db.execute(
                "DELETE FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )
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

    async def identity_for_login(self, github_login: str) -> IdentityLink | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM identity_links WHERE github_login = ?", (github_login,)
            )
        return None if row is None else self._identity(row)

    @staticmethod
    def _code(row: aiosqlite.Row) -> IdentityCode:
        return IdentityCode(
            row["slack_user_id"],
            row["github_login"],
            row["code_digest"],
            from_text(row["expires_at"]),
            row["checks"],
        )

    async def start_verification(self, code: IdentityCode) -> None:
        check_id(code.slack_user_id, "Slack user id")
        check_github_login(code.github_login)
        async with self._write():
            await self._db.execute(
                """
                INSERT INTO identity_codes (slack_user_id, github_login, code_digest, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    github_login = excluded.github_login,
                    code_digest = excluded.code_digest,
                    expires_at = excluded.expires_at,
                    checks = 0
                """,
                (code.slack_user_id, code.github_login, code.code_digest, to_text(code.expires_at)),
            )

    async def get_verification(self, slack_user_id: str) -> IdentityCode | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )
        return None if row is None else self._code(row)

    async def count_verification_check(self, slack_user_id: str) -> int:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE identity_codes SET checks = checks + 1 WHERE slack_user_id = ?",
                (slack_user_id,),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"no pending code for {slack_user_id}")
            row = await self._one(
                "SELECT checks FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )
        return int(_found(row)["checks"])

    async def drop_verification(self, slack_user_id: str) -> None:
        async with self._write():
            await self._db.execute(
                "DELETE FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )

    async def finish_verification(
        self, slack_user_id: str, *, github_login: str, github_id: int
    ) -> Verified:
        check_id(slack_user_id, "Slack user id")
        check_github_login(github_login)
        now = to_text(self._clock())
        async with self._write():
            await self._db.execute(
                "DELETE FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )
            other = await self._one(
                "SELECT slack_user_id FROM identity_links WHERE github_login = ?"
                " AND slack_user_id != ?",
                (github_login, slack_user_id),
            )
            displaced = None if other is None else str(other["slack_user_id"])
            if displaced is not None:
                await self._db.execute(
                    "DELETE FROM identity_codes WHERE slack_user_id = ?", (displaced,)
                )
                await self._db.execute(
                    "DELETE FROM identity_links WHERE slack_user_id = ?", (displaced,)
                )
                await self._db.execute(
                    "INSERT INTO identity_unlinks (id, slack_user_id, removed_by, removed_at)"
                    " VALUES (?, ?, ?, ?)",
                    (secrets.token_hex(8), displaced, VERIFICATION_REMOVER, now),
                )
            await self._db.execute(
                """
                INSERT INTO identity_links
                    (slack_user_id, github_login, verified, linked_at, verified_at, github_id)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    github_login = excluded.github_login,
                    verified = 1,
                    linked_at = excluded.linked_at,
                    verified_at = excluded.verified_at,
                    github_id = excluded.github_id,
                    linked_by = 'self'
                """,
                (slack_user_id, github_login, now, now, github_id),
            )
            row = await self._one(
                "SELECT * FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
        return Verified(self._identity(_found(row)), displaced)

    async def fill_github_id(self, slack_user_id: str, github_id: int) -> IdentityLink | None:
        if type(github_id) is not int or github_id < 1:
            raise ValueError("a GitHub account id is a positive whole number")
        async with self._write():
            await self._db.execute(
                "UPDATE identity_links SET github_id = ? WHERE slack_user_id = ?"
                " AND github_id IS NULL",
                (github_id, slack_user_id),
            )
            row = await self._one(
                "SELECT * FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
        return None if row is None else self._identity(row)

    async def admin_link(
        self, slack_user_id: str, github_login: str, github_id: int, *, replace: bool = False
    ) -> Verified:
        check_id(slack_user_id, "Slack user id")
        check_github_login(github_login)
        if type(github_id) is not int or github_id < 1:
            raise ValueError("a GitHub account id is a positive whole number")
        now = to_text(self._clock())
        async with self._write():
            other = await self._one(
                "SELECT slack_user_id FROM identity_links WHERE github_login = ?"
                " AND slack_user_id != ?",
                (github_login, slack_user_id),
            )
            displaced = None if other is None else str(other["slack_user_id"])
            if displaced is not None:
                if not replace:
                    raise ConflictError(f"GitHub login {github_login} is already linked")
                for table in ("identity_codes", "identity_links"):
                    await self._db.execute(
                        f"DELETE FROM {table} WHERE slack_user_id = ?",
                        (displaced,),
                    )
                await self._db.execute(
                    "INSERT INTO identity_unlinks (id, slack_user_id, removed_by, removed_at)"
                    " VALUES (?, ?, ?, ?)",
                    (secrets.token_hex(8), displaced, OPERATOR_REMOVER, now),
                )
            own = await self._one(
                "SELECT github_login FROM identity_links WHERE slack_user_id = ?",
                (slack_user_id,),
            )
            replaced_own: str | None = None
            if own is not None and str(own["github_login"]).casefold() != github_login.casefold():
                if not replace:
                    raise ConflictError(f"{slack_user_id} has another GitHub login linked")
                replaced_own = str(own["github_login"])
                await self._db.execute(
                    "INSERT INTO identity_unlinks (id, slack_user_id, removed_by, removed_at)"
                    " VALUES (?, ?, ?, ?)",
                    (secrets.token_hex(8), slack_user_id, OPERATOR_REMOVER, now),
                )
            await self._db.execute(
                "DELETE FROM identity_codes WHERE slack_user_id = ?", (slack_user_id,)
            )
            await self._db.execute(
                """
                INSERT INTO identity_links
                    (slack_user_id, github_login, verified, linked_at, verified_at,
                     github_id, linked_by)
                VALUES (?, ?, 0, ?, NULL, ?, 'admin')
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    github_login = excluded.github_login,
                    verified = 0,
                    linked_at = excluded.linked_at,
                    verified_at = NULL,
                    github_id = excluded.github_id,
                    linked_by = 'admin'
                """,
                (slack_user_id, github_login, now, github_id),
            )
            row = await self._one(
                "SELECT * FROM identity_links WHERE slack_user_id = ?", (slack_user_id,)
            )
        return Verified(self._identity(_found(row)), displaced, replaced_own)

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

    # Weekly summaries (#308)

    @staticmethod
    def _summary(row: aiosqlite.Row) -> SummaryPost:
        return SummaryPost(
            channel_id=row["channel_id"],
            slot=from_text(row["slot"]),
            status=row["status"],
            attempts=int(row["attempts"]),
            updated_at=from_text(row["updated_at"]),
            ts=row["ts"],
        )

    async def start_summary(self, channel_id: str, slot: datetime) -> bool:
        check_id(channel_id, "channel id")
        now = self._clock()
        async with self._write():
            row = await self._one(
                "SELECT * FROM summaries WHERE channel_id = ? AND slot = ?",
                (channel_id, to_text(slot)),
            )
            if row is None:
                await self._db.execute(
                    "INSERT INTO summaries (channel_id, slot, status, attempts, updated_at)"
                    " VALUES (?, ?, 'posting', 1, ?)",
                    (channel_id, to_text(slot), to_text(now)),
                )
                return True
            found = self._summary(row)
            stale = found.status == "posting" and now - found.updated_at > SUMMARY_POSTING_STALE
            if (found.status == "failed" or stale) and found.attempts < MAX_SUMMARY_ATTEMPTS:
                await self._db.execute(
                    "UPDATE summaries SET status = 'posting', attempts = attempts + 1,"
                    " updated_at = ? WHERE channel_id = ? AND slot = ?",
                    (to_text(now), channel_id, to_text(slot)),
                )
                return True
            return False

    async def finish_summary(
        self, channel_id: str, slot: datetime, status: SummaryStatus, ts: str | None = None
    ) -> SummaryPost:
        async with self._write():
            cursor = await self._db.execute(
                "UPDATE summaries SET status = ?, ts = COALESCE(?, ts), updated_at = ?"
                " WHERE channel_id = ? AND slot = ?",
                (status, ts, to_text(self._clock()), channel_id, to_text(slot)),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"no summary for {channel_id} at {slot.isoformat()}")
            row = await self._one(
                "SELECT * FROM summaries WHERE channel_id = ? AND slot = ?",
                (channel_id, to_text(slot)),
            )
        return self._summary(_found(row))

    async def get_summary(self, channel_id: str, slot: datetime) -> SummaryPost | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM summaries WHERE channel_id = ? AND slot = ?",
                (channel_id, to_text(slot)),
            )
        return None if row is None else self._summary(row)

    async def set_summary_topics(
        self, channel_id: str, slot: datetime, topic_ids: Collection[str]
    ) -> None:
        async with self._write():
            # This try's list replaces any earlier one, and older summaries' go.
            await self._db.execute(
                "DELETE FROM summary_topics WHERE channel_id = ? AND slot <= ?",
                (channel_id, to_text(slot)),
            )
            await self._db.executemany(
                "INSERT INTO summary_topics (channel_id, slot, topic_id) VALUES (?, ?, ?)",
                [(channel_id, to_text(slot), topic_id) for topic_id in sorted(set(topic_ids))],
            )

    async def summary_topics(self, channel_id: str, slot: datetime) -> frozenset[str]:
        async with self._lock:
            rows = await self._all(
                "SELECT topic_id FROM summary_topics WHERE channel_id = ? AND slot = ?",
                (channel_id, to_text(slot)),
            )
        return frozenset(row["topic_id"] for row in rows)

    async def set_summary_items(
        self, channel_id: str, slot: datetime, topic_ids: Sequence[str]
    ) -> None:
        async with self._write():
            await self._db.execute(
                "DELETE FROM summary_items WHERE channel_id = ? AND slot <= ?",
                (channel_id, to_text(slot)),
            )
            await self._db.executemany(
                "INSERT INTO summary_items (channel_id, slot, position, topic_id)"
                " VALUES (?, ?, ?, ?)",
                [
                    (channel_id, to_text(slot), number, topic_id)
                    for number, topic_id in enumerate(topic_ids, start=1)
                ],
            )

    async def summary_items(self, channel_id: str, slot: datetime) -> list[str]:
        async with self._lock:
            rows = await self._all(
                "SELECT topic_id FROM summary_items WHERE channel_id = ? AND slot = ?"
                " ORDER BY position",
                (channel_id, to_text(slot)),
            )
        return [row["topic_id"] for row in rows]

    async def is_summary_message(self, channel_id: str, ts: str) -> bool:
        async with self._lock:
            row = await self._one(
                "SELECT 1 FROM summaries WHERE channel_id = ? AND ts = ?", (channel_id, ts)
            )
        return row is not None

    async def summary_for_thread(self, channel_id: str, ts: str) -> SummaryPost | None:
        async with self._lock:
            row = await self._one(
                "SELECT * FROM summaries WHERE channel_id = ? AND ts = ? AND status = 'posted'",
                (channel_id, ts),
            )
        return None if row is None else self._summary(row)

    @staticmethod
    def _deferral(row: aiosqlite.Row) -> SummaryDeferral:
        return SummaryDeferral(
            topic_id=row["topic_id"],
            channel_id=row["channel_id"],
            until_slot=from_text(row["until_slot"]),
            by=row["deferred_by"],
            at=from_text(row["deferred_at"]),
        )

    async def defer_topic(
        self, topic_id: str, channel_id: str, until_slot: datetime, *, by: str
    ) -> SummaryDeferral:
        check_id(by, "Slack user id")
        async with self._write():
            await self._db.execute(
                "INSERT INTO summary_deferrals"
                " (topic_id, channel_id, until_slot, deferred_by, deferred_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (topic_id, until_slot) DO NOTHING",
                (topic_id, channel_id, to_text(until_slot), by, to_text(self._clock())),
            )
            row = await self._one(
                "SELECT * FROM summary_deferrals WHERE topic_id = ? AND until_slot = ?",
                (topic_id, to_text(until_slot)),
            )
        return self._deferral(_found(row))

    async def deferred_topics(self, channel_id: str, slot: datetime) -> list[SummaryDeferral]:
        async with self._lock:
            rows = await self._all(
                "SELECT * FROM summary_deferrals WHERE channel_id = ? AND until_slot = ?"
                " ORDER BY deferred_at, topic_id",
                (channel_id, to_text(slot)),
            )
        return [self._deferral(row) for row in rows]

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
        if kind in (RunKind.DRAFTING, RunKind.QUESTION):
            # One queued run of the kind per thread: a second question in it, from
            # anyone, waits for the same run (#57).
            row = await self._one(
                "SELECT * FROM runs WHERE kind = ? AND status = 'queued'"
                " AND channel_id = ? AND thread_ts = ?",
                (kind.value, channel_id, thread_ts),
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

    async def tool_call_results(self, run_id: str, tools: Collection[str]) -> list[str]:
        names = list(tools)
        if not names:
            return []
        marks = ", ".join("?" for _ in names)
        async with self._lock:
            rows = await self._all(
                f"SELECT result_json FROM tool_calls WHERE run_id = ? AND tool IN ({marks}) "
                "ORDER BY seq",
                (run_id, *names),
            )
        return [str(row["result_json"]) for row in rows]

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

    async def count_run_calls(self, run_id: str, tool: str, *, since: datetime) -> int:
        async with self._lock:
            row = await self._one(
                "SELECT count(*) AS n FROM tool_calls WHERE run_id = ? AND tool = ?"
                " AND called_at >= ?",
                (run_id, tool, to_text(since)),
            )
        return int(row["n"]) if row is not None else 0

    async def count_runs(self, channel_id: str, kind: RunKind, *, since: datetime) -> int:
        async with self._lock:
            row = await self._one(
                "SELECT count(*) AS n FROM runs WHERE channel_id = ? AND kind = ?"
                " AND created_at >= ?",
                (channel_id, kind.value, to_text(since)),
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
