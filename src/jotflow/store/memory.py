"""An in-memory :class:`~jotflow.store.base.Store` for tests.

It follows the same rules as the SQLite store and passes the same contract
tests. Nothing survives the process.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime, timedelta

from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    MessageRef,
    ProposalRecord,
    ProposalState,
    Run,
    RunKind,
    RunStatus,
    Topic,
)
from jotflow.store.base import (
    ACTIVE_RUN_STATUSES,
    DEFAULT_RUN_TIMEOUTS,
    ChannelModeSetting,
    Clock,
    ConflictError,
    IdentityLink,
    LinkSource,
    MessageLink,
    NotFoundError,
    PendingThread,
    Recovery,
    ToolCallRecord,
    TopicRecord,
    check_clearable,
    check_finish,
    check_github_login,
    check_id,
    check_new_proposal,
    check_run_request,
    cleared_draft,
    plan_transition,
    to_text,
    utc_now,
)
from jotflow.store.titles import clean_title


class MemoryStore:
    def __init__(self, clock: Clock = utc_now) -> None:
        self._source = clock
        self._lock = asyncio.Lock()
        self._messages: dict[tuple[str, str], MessageLink] = {}
        self._topics: dict[str, TopicRecord] = {}
        self._proposals: dict[str, ProposalRecord] = {}
        self._identities: dict[str, IdentityLink] = {}
        self._modes: dict[str, ChannelModeSetting] = {}
        self._runs: dict[str, Run] = {}
        self._pending: dict[tuple[str, str], datetime] = {}
        self._calls: list[ToolCallRecord] = []
        self._packets: dict[tuple[str, str, str], MessageRef] = {}

    async def close(self) -> None:
        return None

    def _clock(self) -> datetime:
        now = self._source()
        to_text(now)  # refuses naive times, as the SQLite store does
        return now

    # Slack message pointers

    def _with_messages(self, record: TopicRecord) -> TopicRecord:
        messages = sorted(
            (link.ref for link in self._messages.values() if link.topic_id == record.topic.id),
            key=lambda ref: ref.ts,
        )
        return replace(record, topic=record.topic.model_copy(update={"messages": messages}))

    def _decide(self, ref: MessageRef, topic_id: str | None, by: LinkSource) -> MessageLink:
        check_id(ref.channel_id, "channel id")
        check_id(ref.ts, "message timestamp")
        key = (ref.channel_id, ref.ts)
        existing = self._messages.get(key)
        if existing is not None and existing.linked_by == "person" and by == "agent":
            return existing
        now = self._clock()
        link = MessageLink(ref, topic_id, topic_id is None, by, now)
        self._messages[key] = link
        if topic_id is not None:
            self._topics[topic_id] = replace(self._topics[topic_id], last_activity_at=now)
        return link

    async def link_message(
        self, ref: MessageRef, topic_id: str, *, by: LinkSource = "agent"
    ) -> MessageLink:
        async with self._lock:
            topic = self._topics.get(topic_id)
            if topic is None:
                raise NotFoundError(f"no topic {topic_id}")
            if topic.topic.channel_id != ref.channel_id:
                raise ConflictError("a message can only join a topic in its own channel")
            return self._decide(ref, topic_id, by)

    async def mark_chatter(self, ref: MessageRef, *, by: LinkSource = "agent") -> MessageLink:
        async with self._lock:
            return self._decide(ref, None, by)

    async def get_message(self, channel_id: str, ts: str) -> MessageLink | None:
        return self._messages.get((channel_id, ts))

    # Topics

    async def create_topic(self, channel_id: str, title: str) -> TopicRecord:
        check_id(channel_id, "channel id")
        cleaned = clean_title(title)
        async with self._lock:
            now = self._clock()
            topic = Topic(id=secrets.token_hex(8), channel_id=channel_id, title=cleaned)
            record = TopicRecord(topic, now, now)
            self._topics[topic.id] = record
            return record

    async def get_topic(self, topic_id: str) -> TopicRecord | None:
        record = self._topics.get(topic_id)
        return None if record is None else self._with_messages(record)

    def _update_topic(self, topic_id: str, **changes: object) -> TopicRecord:
        record = self._topics.get(topic_id)
        if record is None:
            raise NotFoundError(f"no topic {topic_id}")
        record = replace(record, topic=record.topic.model_copy(update=changes))
        self._topics[topic_id] = record
        return self._with_messages(record)

    async def rename_topic(self, topic_id: str, title: str) -> TopicRecord:
        cleaned = clean_title(title)
        async with self._lock:
            return self._update_topic(topic_id, title=cleaned)

    async def set_topic_issue(self, topic_id: str, repo: str, issue_number: int) -> TopicRecord:
        if issue_number <= 0:
            raise ValueError("an issue number is positive")
        async with self._lock:
            return self._update_topic(topic_id, repo=repo, issue_number=issue_number)

    async def channel_topics(
        self, channel_id: str, *, active_since: datetime | None = None
    ) -> list[TopicRecord]:
        records = [
            self._with_messages(r)
            for r in self._topics.values()
            if r.topic.channel_id == channel_id
            and (active_since is None or r.last_activity_at >= active_since)
        ]
        return sorted(records, key=lambda r: (r.last_activity_at, r.topic.id), reverse=True)

    # Proposals

    async def add_proposal(self, record: ProposalRecord) -> ProposalRecord:
        check_new_proposal(record)
        async with self._lock:
            if record.id in self._proposals:
                raise ConflictError(f"proposal {record.id} already exists")
            topic_id = record.proposal.topic_id
            if topic_id is not None:
                topic = self._topics.get(topic_id)
                if topic is None:
                    raise NotFoundError(f"no topic {topic_id}")
                if topic.topic.channel_id != record.channel_id:
                    raise ConflictError("a proposal can only be for a topic in its own channel")
            stored = record.model_copy(
                update={"participants": sorted(set(record.participants))}, deep=True
            )
            self._proposals[record.id] = stored
            return stored.model_copy(deep=True)

    async def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        record = self._proposals.get(proposal_id)
        return None if record is None else record.model_copy(deep=True)

    async def thread_proposals(self, channel_id: str, thread_ts: str) -> list[ProposalRecord]:
        found = [
            r.model_copy(deep=True)
            for r in self._proposals.values()
            if r.channel_id == channel_id and r.thread_ts == thread_ts
        ]
        return sorted(found, key=lambda r: (r.created_at, r.id))

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
        async with self._lock:
            record = self._proposals.get(proposal_id)
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
            update: dict[str, object] = {
                "state": changes.state,
                "content_hash": changes.content_hash,
                "approved_by": changes.approved_by,
                "approved_at": changes.approved_at,
                "result": changes.result,
            }
            if changes.clear_draft:
                update |= self._cleared(record)
            updated = record.model_copy(update=update, deep=True)
            self._proposals[proposal_id] = updated
            return updated.model_copy(deep=True)

    def _cleared(self, record: ProposalRecord) -> dict[str, object]:
        return {"proposal": cleared_draft(record.proposal), "draft_cleared_at": self._clock()}

    async def set_card_ts(self, proposal_id: str, ts: str) -> ProposalRecord:
        check_id(ts, "card ts")
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            record = record.model_copy(update={"card_ts": ts}, deep=True)
            self._proposals[proposal_id] = record
            return record.model_copy(deep=True)

    async def clear_draft(self, proposal_id: str) -> ProposalRecord:
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            check_clearable(record)
            if record.draft_cleared_at is None:
                record = record.model_copy(update=self._cleared(record), deep=True)
                self._proposals[proposal_id] = record
            return record.model_copy(deep=True)

    async def supersede_pending(
        self, channel_id: str, thread_ts: str, *, keep_id: str
    ) -> list[str]:
        async with self._lock:
            ids = sorted(
                r.id
                for r in self._proposals.values()
                if r.channel_id == channel_id
                and r.thread_ts == thread_ts
                and r.state is ProposalState.PENDING
                and r.id != keep_id
            )
            for proposal_id in ids:
                record = self._proposals[proposal_id]
                self._proposals[proposal_id] = record.model_copy(
                    update={"state": ProposalState.SUPERSEDED, **self._cleared(record)}, deep=True
                )
            return ids

    # Identity links

    async def link_identity(self, slack_user_id: str, github_login: str) -> IdentityLink:
        check_id(slack_user_id, "Slack user id")
        check_github_login(github_login)
        async with self._lock:
            for other in self._identities.values():
                if (
                    other.github_login.casefold() == github_login.casefold()
                    and other.slack_user_id != slack_user_id
                ):
                    raise ConflictError(f"GitHub login {github_login} is already linked")
            link = IdentityLink(slack_user_id, github_login, False, self._clock())
            self._identities[slack_user_id] = link
            return link

    async def get_identity(self, slack_user_id: str) -> IdentityLink | None:
        return self._identities.get(slack_user_id)

    async def unlink_identity(self, slack_user_id: str) -> bool:
        async with self._lock:
            return self._identities.pop(slack_user_id, None) is not None

    # Channel modes

    async def set_channel_mode(
        self, channel_id: str, mode: ChannelMode, *, changed_by: str
    ) -> ChannelModeSetting:
        check_id(channel_id, "channel id")
        check_id(changed_by, "Slack user id")
        async with self._lock:
            setting = ChannelModeSetting(channel_id, ChannelMode(mode), changed_by, self._clock())
            self._modes[channel_id] = setting
            return setting

    async def get_channel_mode(self, channel_id: str) -> ChannelModeSetting | None:
        return self._modes.get(channel_id)

    # Runs (D8, #53): the same rules the SQLite indexes enforce, checked by hand.

    def _active_run_for(
        self, kind: RunKind, channel_id: str, thread_ts: str, proposal_id: str | None
    ) -> Run | None:
        for run in self._runs.values():
            if run.kind is not kind:
                continue
            if (
                kind is RunKind.DRAFTING
                and run.status is RunStatus.QUEUED
                and (run.channel_id, run.thread_ts) == (channel_id, thread_ts)
            ):
                return run
            if (
                kind is RunKind.EXECUTION
                and run.status in ACTIVE_RUN_STATUSES
                and run.proposal_id == proposal_id
            ):
                return run
        return None

    def _insert_run(
        self,
        kind: RunKind,
        channel_id: str,
        thread_ts: str,
        proposal_id: str | None,
        seconds: int,
    ) -> Run:
        now = self._clock()
        run = Run(
            id=secrets.token_hex(8),
            kind=kind,
            channel_id=channel_id,
            thread_ts=thread_ts,
            proposal_id=proposal_id,
            timeout_seconds=seconds,
            created_at=now,
            deadline=now + timedelta(seconds=seconds),
        )
        self._runs[run.id] = run
        return run

    def _ordered_runs(self) -> list[Run]:
        # Insertion order breaks ties in created_at, as the id does in SQLite
        # closely enough for a test double: both are oldest first.
        return sorted(self._runs.values(), key=lambda r: r.created_at)

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
        async with self._lock:
            existing = self._active_run_for(kind, channel_id, thread_ts, proposal_id)
            if existing is not None:
                return existing.model_copy()
            return self._insert_run(kind, channel_id, thread_ts, proposal_id, seconds).model_copy()

    async def claim_next_run(
        self, *, max_running: int, kinds: Collection[RunKind] | None = None
    ) -> Run | None:
        if max_running < 1:
            raise ValueError("max_running must be at least 1")
        async with self._lock:
            running = [r for r in self._runs.values() if r.status is RunStatus.RUNNING]
            if len(running) >= max_running:
                return None
            busy = {(r.channel_id, r.thread_ts) for r in running}
            for run in self._ordered_runs():
                if kinds is not None and run.kind not in kinds:
                    continue
                if run.status is RunStatus.QUEUED and (run.channel_id, run.thread_ts) not in busy:
                    now = self._clock()
                    claimed = run.model_copy(
                        update={
                            "status": RunStatus.RUNNING,
                            "started_at": now,
                            "deadline": now + timedelta(seconds=run.timeout_seconds),
                        }
                    )
                    self._runs[run.id] = claimed
                    return claimed.model_copy()
            return None

    async def finish_run(
        self, run_id: str, status: RunStatus, *, error: str | None = None
    ) -> Run | None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            if run.status not in ACTIVE_RUN_STATUSES:
                return None
            bounded = check_finish(run, status, error)
            finished = run.model_copy(
                update={"status": status, "finished_at": self._clock(), "error": bounded}
            )
            self._runs[run_id] = finished
            return finished.model_copy()

    async def get_run(self, run_id: str) -> Run | None:
        run = self._runs.get(run_id)
        return None if run is None else run.model_copy()

    async def arm_thread(self, channel_id: str, thread_ts: str, due_at: datetime) -> None:
        check_id(channel_id, "channel id")
        check_id(thread_ts, "thread ts")
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        async with self._lock:
            self._pending[(channel_id, thread_ts)] = due_at

    async def pending_threads(self) -> list[PendingThread]:
        return [
            PendingThread(channel_id, thread_ts, due)
            for (channel_id, thread_ts), due in sorted(
                self._pending.items(), key=lambda item: (item[1], item[0])
            )
        ]

    async def queue_pending(self, channel_id: str, thread_ts: str) -> Run | None:
        seconds = check_run_request(
            RunKind.DRAFTING, channel_id, thread_ts, None, DEFAULT_RUN_TIMEOUTS[RunKind.DRAFTING]
        )
        async with self._lock:
            if self._pending.pop((channel_id, thread_ts), None) is None:
                return None
            existing = self._active_run_for(RunKind.DRAFTING, channel_id, thread_ts, None)
            if existing is not None:
                return existing.model_copy()
            return self._insert_run(
                RunKind.DRAFTING, channel_id, thread_ts, None, seconds
            ).model_copy()

    async def drop_pending(self, channel_id: str, thread_ts: str) -> None:
        async with self._lock:
            self._pending.pop((channel_id, thread_ts), None)

    async def thread_runs(self, channel_id: str, thread_ts: str) -> list[Run]:
        return [
            r.model_copy()
            for r in self._ordered_runs()
            if (r.channel_id, r.thread_ts) == (channel_id, thread_ts)
        ]

    async def overdue_runs(self) -> list[Run]:
        now = self._clock()
        overdue = [
            r for r in self._runs.values() if r.status is RunStatus.RUNNING and r.deadline < now
        ]
        return [r.model_copy() for r in sorted(overdue, key=lambda r: r.deadline)]

    async def recover_runs(self) -> Recovery:
        async with self._lock:
            runs = [r for r in self._ordered_runs() if r.status in ACTIVE_RUN_STATUSES]
            now = self._clock()
            for run in runs:
                self._runs[run.id] = run.model_copy(
                    update={"status": RunStatus.INTERRUPTED, "finished_at": now}
                )
            requeued: list[str] = []
            threads: set[tuple[str, str]] = set()
            timeouts: dict[str, int] = {}
            for run in runs:
                if run.kind is RunKind.DRAFTING and (run.channel_id, run.thread_ts) not in threads:
                    threads.add((run.channel_id, run.thread_ts))
                    fresh = self._insert_run(
                        run.kind, run.channel_id, run.thread_ts, None, run.timeout_seconds
                    )
                    requeued.append(fresh.id)
                elif run.kind is RunKind.EXECUTION and run.proposal_id is not None:
                    timeouts.setdefault(run.proposal_id, run.timeout_seconds)
            waiting_states = (
                ProposalState.APPROVED,
                ProposalState.EXECUTING,
                ProposalState.RECONCILING,
            )
            waiting = sorted(
                (p for p in self._proposals.values() if p.state in waiting_states),
                key=lambda p: (p.created_at, p.id),
            )
            reconciling: list[str] = []
            for record in waiting:
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
                    self._proposals[record.id] = record.model_copy(
                        update={"state": changes.state}, deep=True
                    )
                    reconciling.append(record.id)
                seconds = timeouts.get(
                    record.id, int(DEFAULT_RUN_TIMEOUTS[RunKind.EXECUTION].total_seconds())
                )
                fresh = self._insert_run(
                    RunKind.EXECUTION, record.channel_id, record.thread_ts, record.id, seconds
                )
                requeued.append(fresh.id)
            return Recovery(
                interrupted=tuple(run.id for run in runs),
                requeued=tuple(requeued),
                reconciling=tuple(reconciling),
            )

    # Write-tool ledger and packet membership

    async def find_tool_call(self, run_id: str, tool: str, args_hash: str) -> str | None:
        for call in self._calls:
            if (call.run_id, call.tool, call.args_hash) == (run_id, tool, args_hash):
                return call.result_json
        return None

    async def record_tool_call(self, call: ToolCallRecord) -> int:
        check_id(call.run_id, "run id")
        check_id(call.channel_id, "channel id")
        to_text(call.called_at)
        async with self._lock:
            if any(
                (c.run_id, c.tool, c.args_hash) == (call.run_id, call.tool, call.args_hash)
                for c in self._calls
            ):
                raise ConflictError(f"run {call.run_id} already recorded this {call.tool} call")
            self._calls.append(call)
            return sum(1 for c in self._calls if c.run_id == call.run_id)

    async def count_tool_calls(self, run_id: str, tools: Collection[str]) -> int:
        return sum(1 for c in self._calls if c.run_id == run_id and c.tool in tools)

    async def count_thread_calls(
        self, channel_id: str, thread_ts: str | None, tool: str, *, since: datetime
    ) -> int:
        return sum(
            1
            for c in self._calls
            if (c.channel_id, c.thread_ts, c.tool) == (channel_id, thread_ts, tool)
            and c.called_at >= since
        )

    async def record_packet(self, run_id: str, refs: Collection[MessageRef]) -> None:
        check_id(run_id, "run id")
        async with self._lock:
            for ref in refs:
                check_id(ref.channel_id, "channel id")
                check_id(ref.ts, "message timestamp")
                key = (run_id, ref.channel_id, ref.ts)
                self._packets.setdefault(key, ref.model_copy(update={"permalink": None}))

    async def packet_ref(self, run_id: str, channel_id: str, ts: str) -> MessageRef | None:
        found = self._packets.get((run_id, channel_id, ts))
        return None if found is None else found.model_copy()
