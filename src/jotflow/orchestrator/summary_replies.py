"""Replies in a weekly summary's thread (#318).

A summary's thread is never drafted: its packet would mix every listed idea's
title with the replies. So a reply there never reaches the debouncer, a
question run, keywords or help. Instead:

- **``re: 2``** names the summary's item 2. JotFlow links the reply to that
  idea's topic as the poster's own decision, as ``re: web#42`` does, confirms
  with its reaction, and posts one fixed note in the idea's own thread, naming
  the replier as plain text, never a mention, with a link to the reply. No reply
  text is copied, and nothing is drafted.
- **Only the channel's latest summary routes,** as Not this week. A number in an
  older summary's thread, a number it didn't list, or an idea removed since gets
  a fixed private reply.
- **Anything else** is left alone and not drafted, and its author is told once
  per summary to post new ideas in the channel.

Only members of this workspace route, never guests or external users. Private
replies are limited per person, so a flood of replies can't flood them.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from jotflow.execution.writes import permalink
from jotflow.models import IssueProposal, MessageRef, ProposalRecord, ProposalState
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.people import Person
from jotflow.orchestrator.summary import _newest_create, latest_slot
from jotflow.policy import Policy
from jotflow.reactions import CONFIRM
from jotflow.store.base import Store, SummaryPost, utc_now
from jotflow.text import without_unsafe_chars

log = logging.getLogger("jotflow.summary")

_NUMBER = re.compile(r"\s*([0-9]{1,3})(?![0-9])")
_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")
MAX_TOLD = 5
TOLD_WINDOW_SECONDS = 600.0

PASSED = "That summary has passed. Reply in the idea's own thread instead."
NOT_OPEN = "That idea is no longer open, so nothing was linked."
NEW_IDEAS_HERE = (
    "To suggest a new idea, post it in the channel, not in a reply to JotFlow's post. "
    "To add to an idea in the summary, start your reply with re: and its number, such as re: 2."
)
NEEDS_NUMBER = "In a summary's thread, re: takes an item's number, such as re: 2."
# Under one of JotFlow's own top-level posts with no posted summary on record: the
# help guide, a mode announcement, the live check's post, or a summary not yet
# recorded. So these never assume the post is a summary.
POST_IN_CHANNEL = "To suggest a new idea, post it in the channel, not in a reply to JotFlow's post."
OWN_POST_RE = "Nothing was linked from this reply. To add to an idea, reply in its own thread."
# The same, in a channel with a weekly summary, where re: N works in its thread.
OWN_POST_RE_SUMMARY = (
    OWN_POST_RE + " Or reply in the latest weekly summary's thread with re: and its number."
)


def no_item(number: int) -> str:
    return f"That summary has no item {number}."


def note_text(name: str, link: str | None) -> str:
    """The note in the idea's thread: JotFlow's words, the name as plain text."""
    where = f" <{link}|See the reply.>" if link else ""
    return f"{name} replied about this in the weekly summary.{where}"


def plain_name(name: str | None) -> str:
    """A display name safe in mrkdwn: no markup, links or mentions."""
    cleaned = without_unsafe_chars(name or "")
    for mark in "<>&*_~`|":
        cleaned = cleaned.replace(mark, "")
    cleaned = " ".join(cleaned.split())[:80]
    return cleaned or "Someone"


class RepliesSlack(Protocol):
    async def person(self, user_id: str) -> Person: ...

    async def name(self, user_id: str) -> str | None: ...

    async def workspace_url(self) -> str | None: ...

    async def ephemeral(self, channel_id: str, user_id: str, thread_ts: str, text: str) -> None: ...

    async def react(self, channel_id: str, ts: str, name: str) -> None: ...

    async def note(self, channel_id: str, thread_ts: str, text: str) -> None: ...


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _removed(record: ProposalRecord | None) -> bool:
    """The summary's rule (#315): rejected, superseded or cleared other than by filing."""
    if record is None:
        return True
    if record.state in (ProposalState.REJECTED, ProposalState.SUPERSEDED):
        return True
    return record.draft_cleared_at is not None and record.state is not ProposalState.APPLIED


class SummaryReplies:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        slack: RepliesSlack,
        *,
        bot_user_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot_user_id
        self._store = store
        self._policy = policy
        self._slack = slack
        self._clock = clock
        self._monotonic = monotonic
        self._told: dict[str, deque[float]] = {}
        self._pointed: set[tuple[str, datetime, str]] = set()
        # One note per person per item per summary: later replies are only reacted to.
        self._noted: set[tuple[str, datetime, str, str]] = set()
        # Pointers under JotFlow's own posts with no summary row, by thread.
        self._pointed_threads: set[tuple[str, str, str]] = set()

    async def summary_thread(self, event: Mapping[str, Any]) -> SummaryPost | None:
        """The posted summary whose thread this reply is in, or None."""
        channel = _str(event.get("channel"))
        ts = _str(event.get("ts"))
        thread = _str(event.get("thread_ts"))
        if channel is None or thread is None or ts == thread or not _TS.fullmatch(thread):
            return None
        return await self._store.summary_for_thread(channel, thread)

    def under_own_post(self, event: Mapping[str, Any]) -> bool:
        """A reply under one of JotFlow's own top-level posts, by Slack's parent_user_id.

        JotFlow posts cards, questions and notes inside threads. Its only top-level
        posts are:

        - the weekly summary;
        - the help guide;
        - the announcement of a mode change, such as pause, resume (back to
          propose) or auto;
        - ``jotflow check``'s live test post.

        A reply under any of them is never drafted. This also holds before a
        summary's row is marked posted, or after a crash left it failed.
        """
        thread = _str(event.get("thread_ts"))
        return (
            self._bot is not None
            and thread is not None
            and thread != _str(event.get("ts"))
            and event.get("parent_user_id") == self._bot
        )

    async def on_unrecorded(self, event: Mapping[str, Any], *, asked: bool = False) -> str:
        """A reply under JotFlow's own post with no posted summary on record: nothing
        routes, and it's never drafted. The post may be any of JotFlow's top-level
        posts, so the private replies don't call it a summary."""
        user = _str(event.get("user"))
        channel = _str(event.get("channel"))
        thread = _str(event.get("thread_ts"))
        if user is None or channel is None or thread is None:
            return "ignored"
        person = await self._slack.person(user)
        if person.guest or person.external:
            return "ignored"
        keyword, _ = split_keyword(str(event.get("text") or ""))
        if keyword == "re":
            listed = self._policy.channels.get(channel)
            summaries = listed is not None and listed.summary is not None
            await self._tell(
                channel, user, thread, OWN_POST_RE_SUMMARY if summaries else OWN_POST_RE
            )
            return "refused"
        if asked or (channel, thread, user) in self._pointed_threads:
            return "left"
        self._pointed_threads.add((channel, thread, user))
        await self._tell(channel, user, thread, POST_IN_CHANNEL)
        return "pointed"

    def _prune(self, slot: datetime) -> None:
        """Forget older summaries' pointers and notes: only the latest routes."""
        self._pointed = {key for key in self._pointed if key[1] >= slot}
        self._noted = {key for key in self._noted if key[1] >= slot}

    async def on_reply(
        self, event: Mapping[str, Any], post: SummaryPost, *, asked: bool = False
    ) -> str:
        """Handle one reply in ``post``'s thread. Returns what was done.

        ``asked``: it was a question to JotFlow, which a question run answers, so it
        needs no pointer to the channel.
        """
        user = _str(event.get("user"))
        ts = _str(event.get("ts"))
        channel = post.channel_id
        thread = post.ts
        if user is None or ts is None or thread is None:
            return "ignored"
        self._prune(post.slot)
        person = await self._slack.person(user)
        if person.guest or person.external:
            return "ignored"  # only members route, and nobody else is told anything
        keyword, rest = split_keyword(str(event.get("text") or ""))
        if keyword != "re":
            if asked or (channel, post.slot, user) in self._pointed:
                return "left"
            self._pointed.add((channel, post.slot, user))
            await self._tell(channel, user, thread, NEW_IDEAS_HERE)
            return "pointed"
        match = _NUMBER.match(rest)
        if match is None:
            await self._tell(channel, user, thread, NEEDS_NUMBER)
            return "refused"
        number = int(match.group(1))
        listed = self._policy.channels.get(channel)
        setting = listed.summary if listed is not None else None
        if setting is None or post.slot != latest_slot(self._clock(), setting):
            await self._tell(channel, user, thread, PASSED)
            return "refused"
        items = await self._store.summary_items(channel, post.slot)
        if not 1 <= number <= len(items):
            await self._tell(channel, user, thread, no_item(number))
            return "refused"
        topic_id = items[number - 1]
        topic = await self._store.get_topic(topic_id)
        record = (
            await _newest_create(self._store, channel, topic_id, topic.topic.messages)
            if topic is not None and topic.topic.channel_id == channel
            else None
        )
        if _removed(record) or record is None or not isinstance(record.proposal, IssueProposal):
            await self._tell(channel, user, thread, NOT_OPEN)
            return "refused"
        ref = MessageRef(channel_id=channel, ts=ts, thread_ts=thread)
        await self._store.link_message(ref, topic_id, by="person")
        await self._slack.react(channel, ts, CONFIRM)
        noted = (channel, post.slot, user, topic_id)
        if noted in self._noted:
            log.info("summary reply %s linked to topic %s; already noted", ts, topic_id)
            return "linked"
        self._noted.add(noted)
        workspace = await self._slack.workspace_url()
        link = permalink(workspace, channel, ts, thread) if workspace else None
        name = plain_name(await self._slack.name(user))
        await self._slack.note(channel, record.thread_ts, note_text(name, link))
        log.info("summary reply %s routed to topic %s by user %s", ts, topic_id, user)
        return "routed"

    async def _tell(self, channel: str, user: str, thread: str, text: str) -> None:
        now = self._monotonic()
        recent = self._told.setdefault(user, deque())
        while recent and now - recent[0] >= TOLD_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= MAX_TOLD:
            return  # a flood of replies can't make JotFlow flood private replies
        recent.append(now)
        await self._slack.ephemeral(channel, user, thread, text)


class BoltRepliesSlack:
    """:class:`RepliesSlack` over slack-sdk's client and the shared people lookup."""

    def __init__(self, client: Any, people: Any, names: Any) -> None:
        self._client = client
        self._people = people
        self._names = names

    async def person(self, user_id: str) -> Person:
        found: Person = await self._people.person(user_id)
        return found

    async def name(self, user_id: str) -> str | None:
        found: str | None = await self._names.name(user_id)
        return found

    async def workspace_url(self) -> str | None:
        url: str | None = await self._people.workspace_url()
        return url

    async def ephemeral(self, channel_id: str, user_id: str, thread_ts: str, text: str) -> None:
        await self._client.chat_postEphemeral(
            channel=channel_id, user=user_id, thread_ts=thread_ts, text=text
        )

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        await self._client.reactions_add(channel=channel_id, timestamp=ts, name=name)

    async def note(self, channel_id: str, thread_ts: str, text: str) -> None:
        await self._client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            link_names=False,
            unfurl_links=False,
            unfurl_media=False,
        )
