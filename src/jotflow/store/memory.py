"""An in-memory :class:`~jotflow.store.base.Store` for tests.

It follows the same rules as the SQLite store and passes the same contract
tests. Nothing survives the process.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime

from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    MessageRef,
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
    TopicRecord,
    check_clearable,
    check_github_login,
    check_id,
    check_new_proposal,
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

    async def thread_proposals(
        self, channel_id: str, thread_ts: str | None
    ) -> list[ProposalRecord]:
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
        self, channel_id: str, thread_ts: str | None, *, keep_id: str
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
