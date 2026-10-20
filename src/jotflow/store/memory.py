"""An in-memory :class:`~jotflow.store.base.Store` for tests.

It follows the same rules as the SQLite store and passes the same contract
tests. Nothing survives the process.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from jotflow.models import (
    ChannelMode,
    Duplicate,
    ExecutionResult,
    IssueProposal,
    MessageRef,
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
    AUTO_APPROVER,
    BOUND_STATES,
    DEFAULT_RUN_TIMEOUTS,
    DUPLICATE_MOVED_ON,
    DUPLICATE_NOT_NEW,
    DUPLICATE_TOPIC_LINKED,
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
    ReactionKeyword,
    ReactionMark,
    Recovery,
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
    check_run_request,
    cleared_draft,
    edited_draft,
    plan_duplicate,
    plan_transition,
    plan_undo,
    to_text,
    utc_now,
)
from jotflow.store.titles import clean_title

_WRITING = frozenset({ProposalState.APPROVED, ProposalState.EXECUTING, ProposalState.RECONCILING})


def _auto_writing(record: ProposalRecord) -> bool:
    """Auto mode's card waits for its write (#28), so it is not due until then."""
    return (
        record.card_ts is None and record.approved_by == AUTO_APPROVER and record.state in _WRITING
    )


class MemoryStore:
    def __init__(self, clock: Clock = utc_now) -> None:
        self._source = clock
        self._lock = asyncio.Lock()
        self._messages: dict[tuple[str, str], MessageLink] = {}
        self._topics: dict[str, TopicRecord] = {}
        self._proposals: dict[str, ProposalRecord] = {}
        self._identities: dict[str, IdentityLink] = {}
        self._unlinks: list[IdentityUnlink] = []
        self._codes: dict[str, IdentityCode] = {}
        self._operators: dict[str, OperatorToken] = {}
        self._modes: dict[str, ChannelModeSetting] = {}
        self._notices: set[tuple[str, str, str]] = set()
        self._marks: dict[tuple[str, str, str, str], ReactionMark] = {}
        self._edits: list[ProposalEdit] = []
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

    async def proposal_for_card(self, channel_id: str, card_ts: str) -> ProposalRecord | None:
        for record in self._proposals.values():
            if record.channel_id == channel_id and record.card_ts == card_ts:
                return record.model_copy(deep=True)
        return None

    async def run_proposals(self, run_id: str) -> list[ProposalRecord]:
        found = [r.model_copy(deep=True) for r in self._proposals.values() if r.run_id == run_id]
        return sorted(found, key=lambda r: (r.created_at, r.id))

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
                "revision": record.revision + 1,
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

    async def edit_proposal(
        self,
        proposal_id: str,
        draft: IssueProposal | UpdateProposal,
        *,
        by: str,
        field: EditField,
    ) -> ProposalRecord:
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            check_edit(record, draft, by, field)
            draft = edited_draft(record, draft)
            updated = record.model_copy(
                update={"proposal": draft, "revision": record.revision + 1}, deep=True
            )
            self._proposals[proposal_id] = updated
            self._edits.append(ProposalEdit(proposal_id, by, field, self._clock()))
            return updated.model_copy(deep=True)

    async def proposal_edits(self, proposal_id: str) -> list[ProposalEdit]:
        return [e for e in self._edits if e.proposal_id == proposal_id]

    async def set_card_ts(self, proposal_id: str, ts: str) -> ProposalRecord:
        check_id(ts, "card ts")
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            record = record.model_copy(update={"card_ts": ts}, deep=True)
            self._proposals[proposal_id] = record
            return record.model_copy(deep=True)

    async def cards_due(self, limit: int = 50) -> list[ProposalRecord]:
        due = [
            r
            for r in self._proposals.values()
            if r.card_due and not self._paused(r.channel_id) and not _auto_writing(r)
        ]
        due.sort(key=lambda r: (r.created_at, r.id))
        return [r.model_copy(deep=True) for r in due[:limit]]

    async def mark_card_shown(self, proposal_id: str, revision: int) -> None:
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            shown = max(record.card_revision or 0, min(revision, record.revision))
            self._proposals[proposal_id] = record.model_copy(update={"card_revision": shown})

    async def threads_due(self, limit: int = 50) -> list[ProposalRecord]:
        due = [
            r
            for r in self._proposals.values()
            if r.state is ProposalState.APPLIED
            and r.thread_told_at is None
            and not self._paused(r.channel_id)
        ]
        due.sort(key=lambda r: (r.created_at, r.id))
        return [r.model_copy(deep=True) for r in due[:limit]]

    async def mark_thread_told(self, proposal_id: str) -> None:
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            if record.thread_told_at is None:
                self._proposals[proposal_id] = record.model_copy(
                    update={"thread_told_at": self._clock()}
                )

    async def clear_draft(self, proposal_id: str) -> ProposalRecord:
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            check_clearable(record)
            if record.draft_cleared_at is None:
                update = {**self._cleared(record), "revision": record.revision + 1}
                record = record.model_copy(update=update, deep=True)
                self._proposals[proposal_id] = record
            return record.model_copy(deep=True)

    async def mark_duplicate(
        self, proposal_id: str, *, index: int, expected_hash: str, by: str
    ) -> ProposalRecord:
        check_id(by, "Slack user id")
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            related = plan_duplicate(record, index, expected_hash)
            topic_id = record.proposal.topic_id
            topic = None if topic_id is None else self._topics.get(topic_id)
            if topic is None or topic.topic.channel_id != record.channel_id:
                raise DuplicateRefusedError(DUPLICATE_NOT_NEW)
            if topic.topic.issue_number is not None:
                raise DuplicateRefusedError(DUPLICATE_TOPIC_LINKED)
            now = self._clock()
            self._topics[topic.topic.id] = replace(
                topic,
                topic=topic.topic.model_copy(
                    update={"repo": related.repo, "issue_number": related.number}
                ),
                last_activity_at=now,
            )
            duplicate = Duplicate(repo=related.repo, number=related.number, by=by, at=now)
            update = {
                **self._cleared(record),
                "state": ProposalState.REJECTED,
                "duplicate": duplicate,
                "revision": record.revision + 1,
            }
            updated = record.model_copy(update=update, deep=True)
            self._proposals[proposal_id] = updated
            return updated.model_copy(deep=True)

    async def undo_duplicate(self, proposal_id: str, *, by: str) -> ProposalRecord:
        check_id(by, "Slack user id")
        async with self._lock:
            record = self._proposals.get(proposal_id)
            if record is None:
                raise NotFoundError(f"no proposal {proposal_id}")
            duplicate = plan_undo(record)
            topic_id = record.proposal.topic_id
            topic = None if topic_id is None else self._topics.get(topic_id)
            bound = (
                topic is not None
                and topic.topic.repo is not None
                and topic.topic.repo.casefold() == duplicate.repo.casefold()
                and topic.topic.issue_number == duplicate.number
            )
            if topic is None or not bound:
                raise DuplicateRefusedError(DUPLICATE_MOVED_ON)
            # Drafted after the "same as" and still live or written: it relies on
            # the binding, so unbinding would strand it or contradict its write.
            if any(
                p.proposal.topic_id == topic.topic.id
                and p.state in BOUND_STATES
                and p.created_at > duplicate.at
                for p in self._proposals.values()
            ):
                raise DuplicateRefusedError(DUPLICATE_MOVED_ON)
            now = self._clock()
            self._topics[topic.topic.id] = replace(
                topic, topic=topic.topic.model_copy(update={"repo": None, "issue_number": None})
            )
            undone = duplicate.model_copy(update={"undone_by": by, "undone_at": now})
            updated = record.model_copy(
                update={"duplicate": undone, "revision": record.revision + 1}, deep=True
            )
            self._proposals[proposal_id] = updated
            return updated.model_copy(deep=True)

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
                update = {
                    "state": ProposalState.SUPERSEDED,
                    "revision": record.revision + 1,
                    **self._cleared(record),
                }
                self._proposals[proposal_id] = record.model_copy(update=update, deep=True)
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
            self._codes.pop(slack_user_id, None)  # a changed link drops a pending code
            return link

    async def get_identity(self, slack_user_id: str) -> IdentityLink | None:
        return self._identities.get(slack_user_id)

    async def unlink_identity(self, slack_user_id: str) -> bool:
        async with self._lock:
            self._codes.pop(slack_user_id, None)
            return self._identities.pop(slack_user_id, None) is not None

    async def remove_identity(self, slack_user_id: str, *, removed_by: str) -> bool:
        check_id(slack_user_id, "Slack user id")
        check_id(removed_by, "remover")
        async with self._lock:
            self._codes.pop(slack_user_id, None)
            if self._identities.pop(slack_user_id, None) is None:
                return False
            self._unlinks.append(
                IdentityUnlink(secrets.token_hex(8), slack_user_id, removed_by, self._clock())
            )
            return True

    async def identity_unlinks(self) -> list[IdentityUnlink]:
        return sorted(self._unlinks, key=lambda u: u.removed_at, reverse=True)

    async def identity_for_login(self, github_login: str) -> IdentityLink | None:
        wanted = github_login.casefold()
        return next(
            (link for link in self._identities.values() if link.github_login.casefold() == wanted),
            None,
        )

    async def start_verification(self, code: IdentityCode) -> None:
        check_id(code.slack_user_id, "Slack user id")
        check_github_login(code.github_login)
        async with self._lock:
            self._codes[code.slack_user_id] = replace(code, checks=0)

    async def get_verification(self, slack_user_id: str) -> IdentityCode | None:
        return self._codes.get(slack_user_id)

    async def count_verification_check(self, slack_user_id: str) -> int:
        async with self._lock:
            code = self._codes.get(slack_user_id)
            if code is None:
                raise NotFoundError(f"no pending code for {slack_user_id}")
            self._codes[slack_user_id] = replace(code, checks=code.checks + 1)
            return code.checks + 1

    async def drop_verification(self, slack_user_id: str) -> None:
        async with self._lock:
            self._codes.pop(slack_user_id, None)

    async def finish_verification(
        self, slack_user_id: str, *, github_login: str, github_id: int
    ) -> Verified:
        check_id(slack_user_id, "Slack user id")
        check_github_login(github_login)
        async with self._lock:
            self._codes.pop(slack_user_id, None)
            displaced: str | None = None
            for other in list(self._identities.values()):
                if (
                    other.github_login.casefold() == github_login.casefold()
                    and other.slack_user_id != slack_user_id
                ):
                    displaced = other.slack_user_id
                    del self._identities[other.slack_user_id]
                    self._codes.pop(other.slack_user_id, None)
                    self._unlinks.append(
                        IdentityUnlink(
                            secrets.token_hex(8), displaced, VERIFICATION_REMOVER, self._clock()
                        )
                    )
            now = self._clock()
            link = IdentityLink(slack_user_id, github_login, True, now, now, github_id)
            self._identities[slack_user_id] = link
            return Verified(link, displaced)

    # Operator tokens

    async def add_operator_token(self, token: OperatorToken) -> OperatorToken:
        async with self._lock:
            if any(
                t.name == token.name or t.digest == token.digest for t in self._operators.values()
            ):
                raise ConflictError(f"an operator token named {token.name} already exists")
            self._operators[token.id] = token
            return token

    async def operator_tokens(self) -> list[OperatorToken]:
        return sorted(self._operators.values(), key=lambda t: (t.created_at, t.id), reverse=True)

    async def revoke_operator_token(self, name: str) -> OperatorToken | None:
        async with self._lock:
            for token in self._operators.values():
                if token.name == name and token.revoked_at is None:
                    revoked = replace(token, revoked_at=self._clock())
                    self._operators[token.id] = revoked
                    return revoked
            return None

    async def touch_operator_tokens(self, used: Mapping[str, datetime]) -> None:
        async with self._lock:
            for token_id, when in used.items():
                token = self._operators.get(token_id)
                if token is not None and (token.last_used_at is None or when > token.last_used_at):
                    self._operators[token_id] = replace(token, last_used_at=when)

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

    def _paused(self, channel_id: str) -> bool:
        """Nothing runs and no card is drawn in a paused channel (#28)."""
        setting = self._modes.get(channel_id)
        return setting is not None and setting.mode is ChannelMode.PAUSED

    async def get_channel_mode(self, channel_id: str) -> ChannelModeSetting | None:
        return self._modes.get(channel_id)

    async def add_reaction_mark(
        self, channel_id: str, ts: str, user_id: str, keyword: ReactionKeyword
    ) -> bool:
        check_mark(channel_id, ts, user_id, keyword)
        async with self._lock:
            key = (channel_id, ts, user_id, keyword)
            if key in self._marks:
                return False
            self._marks[key] = ReactionMark(channel_id, ts, user_id, keyword, self._clock())
            return True

    async def remove_reaction_mark(
        self, channel_id: str, ts: str, user_id: str, keyword: ReactionKeyword
    ) -> bool:
        check_mark(channel_id, ts, user_id, keyword)
        async with self._lock:
            return self._marks.pop((channel_id, ts, user_id, keyword), None) is not None

    async def reaction_marks(self, channel_id: str, ts: str) -> list[ReactionMark]:
        found = [m for m in self._marks.values() if (m.channel_id, m.ts) == (channel_id, ts)]
        return sorted(found, key=lambda m: (m.marked_at, m.user_id, m.keyword))

    async def unmark_chatter(self, ref: MessageRef) -> bool:
        async with self._lock:
            key = (ref.channel_id, ref.ts)
            existing = self._messages.get(key)
            if existing is None or not existing.chatter or existing.linked_by != "person":
                return False
            del self._messages[key]
            return True

    async def claim_notice(self, channel_id: str, user_id: str, kind: NoticeKind) -> bool:
        check_notice(channel_id, user_id, kind)
        async with self._lock:
            key = (channel_id, user_id, kind)
            if key in self._notices:
                return False
            self._notices.add(key)
            return True

    async def release_notice(self, channel_id: str, user_id: str, kind: NoticeKind) -> None:
        check_notice(channel_id, user_id, kind)
        async with self._lock:
            self._notices.discard((channel_id, user_id, kind))

    # Runs (D8, #53): the same rules the SQLite indexes enforce, checked by hand.

    def _active_run_for(
        self, kind: RunKind, channel_id: str, thread_ts: str, proposal_id: str | None
    ) -> Run | None:
        for run in self._runs.values():
            if run.kind is not kind:
                continue
            if (
                kind in (RunKind.DRAFTING, RunKind.QUESTION)
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
                if self._paused(run.channel_id):
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
            root = self._messages.get((channel_id, thread_ts))
            if root is not None and root.chatter and root.linked_by == "person":
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
                        update={"state": changes.state, "revision": record.revision + 1},
                        deep=True,
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

    async def count_runs(self, channel_id: str, kind: RunKind, *, since: datetime) -> int:
        return sum(
            1
            for r in self._runs.values()
            if r.channel_id == channel_id and r.kind is kind and r.created_at >= since
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
