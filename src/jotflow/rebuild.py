"""Rebuild the store's index from JotFlow's own Slack replies (#38).

The store is a cache. Everything it most needs, which thread and which
messages belong to which GitHub issue, JotFlow also left in Slack: after every
write it replied in the thread with ``Created <issue url|nick#N>`` or
``Updated ...`` (:func:`jotflow.orchestrator.thread_notice.reply_text`). This
walks each configured channel, finds those replies among the bot's own
messages, and restores:

- a topic per issue, linked to its repo and number, titled ``nick#N``
- the people's messages in that thread, linked to the topic, or marked as
  chatter when they start with ``skip:``
- the channel's latest mode, from JotFlow's own announcement
- whether the channel's guide was already posted, so it isn't posted twice

Message text is read in memory only: to recognise the bot's own replies, and to
see a ``skip:`` prefix, as ingest does. Nothing but ids and pointers is stored
or logged. GitHub is never touched, because JotFlow holds no GitHub token (#256
is the optional follow-up through the read-only runner).

The walk (:func:`collect`) only reads, so ``--dry-run`` and the real run share it.
:func:`apply` then writes, never overwriting what the store already holds.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jotflow.models import ChannelMode, MarkedIssue, MessageRef
from jotflow.orchestrator.ingest import NEW_MESSAGE_SUBTYPES, BotIdentity
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.packet import Budget, Sleep, with_backoff
from jotflow.policy import Policy
from jotflow.store import ConflictError, Store

log = logging.getLogger("jotflow.rebuild")

# JotFlow's thread reply after a write, exactly as thread_notice.reply_text writes it.
_REPLY = re.compile(
    r"(?:Created|Updated) <https://github\.com/(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)"
    r"/issues/(?P<number>[0-9]+)\|[^>]*>"
)
# JotFlow's mode announcement, as modes.ModeCommand writes its first line.
_MODE = re.compile(r"set JotFlow to (?P<mode>propose|auto|paused) mode in this channel\.")
# A sentence only the channel guide says (help.guide_text).
_GUIDE = "turns ideas and bugs posted here into GitHub issues, after a person approves."
PAGE_SIZE = 200
# Rate-limit waiting one channel may spend, shared across its pages: a long
# history takes minutes, but a channel that stays limited stops cleanly.
CHANNEL_BUDGET_SECONDS = 900.0
RETRY_ATTEMPTS = 8


class RebuildSlack(Protocol):
    """The three Slack reads rebuild needs, all within the bot token's existing scopes."""

    async def history(
        self, channel: str, *, cursor: str | None, oldest: str | None, limit: int
    ) -> Mapping[str, Any]: ...

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]: ...


@dataclass
class ThreadPlan:
    """What one thread restores: its issues in order, and where each message goes."""

    thread_ts: str
    issues: list[tuple[str, int]] = field(default_factory=list)
    # (ts, index into issues) for an idea; (ts, None) for a skip: message.
    messages: list[tuple[str, int | None]] = field(default_factory=list)
    # JotFlow replies naming a repo the channel no longer feeds: not restored.
    unknown_repos: int = 0


@dataclass
class ChannelPlan:
    channel_id: str
    threads: list[ThreadPlan] = field(default_factory=list)
    mode: ChannelMode | None = None
    guide_posted: bool = False
    messages_read: int = 0
    unknown_repos: int = 0
    # Every person's message the walk saw: ts -> its thread, or None at top level.
    # --from-github links only these (#256), so an issue can't name others.
    seen: dict[str, str | None] = field(default_factory=dict)

    @property
    def issue_count(self) -> int:
        return len({issue for thread in self.threads for issue in thread.issues})


@dataclass
class Restored:
    """Counts only, for the summary."""

    topics: int = 0
    reused_topics: int = 0
    linked: int = 0
    chatter: int = 0
    kept: int = 0
    mode: bool = False
    guide: bool = False


def _is_own(message: Mapping[str, Any], bot: BotIdentity) -> bool:
    if message.get("user") == bot.user_id:
        return True
    return bot.bot_id is not None and message.get("bot_id") == bot.bot_id


def _is_person(message: Mapping[str, Any], bot: BotIdentity) -> bool:
    if _is_own(message, bot) or message.get("bot_id"):
        return False
    return message.get("subtype") in NEW_MESSAGE_SUBTYPES and isinstance(message.get("ts"), str)


def _text(message: Mapping[str, Any]) -> str:
    text = message.get("text")
    return text if isinstance(text, str) else ""


async def _pages(
    fetch: Callable[[str | None], Awaitable[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await fetch(cursor)
        messages = page.get("messages") or []
        out.extend(m for m in messages if isinstance(m, Mapping))
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return out


def plan_thread(
    thread_ts: str,
    messages: Sequence[Mapping[str, Any]],
    bot: BotIdentity,
    repos: Collection[str] | Callable[[str], bool] | None = None,
) -> ThreadPlan:
    """Each person's message goes to the issue of the first JotFlow reply at or after it.

    Messages after the last reply go to that last issue: they are follow-ups,
    which JotFlow would have proposed as updates to it. With ``repos``, a reply
    naming any other repo is skipped and counted, so a repo since removed from
    jotflow.toml doesn't come back. ``repos`` may instead be a test, for a channel
    whose scopes reach repos it doesn't list (#236).
    """
    allowed = _allowed(repos)
    plan = ThreadPlan(thread_ts)
    waiting: list[str] = []
    ordered = sorted(messages, key=lambda m: float(str(m.get("ts") or 0)))
    pending: list[tuple[str, int | None]] = []
    for message in ordered:
        ts = str(message.get("ts") or "")
        if _is_own(message, bot):
            match = _REPLY.search(_text(message))
            if match is None:
                continue
            if allowed is not None and not allowed(match["repo"]):
                plan.unknown_repos += 1
                continue
            issue = (match["repo"], int(match["number"]))
            if issue not in plan.issues:
                plan.issues.append(issue)
            index = plan.issues.index(issue)
            pending.extend((t, index) for t in waiting)
            waiting = []
        elif _is_person(message, bot):
            keyword, _ = split_keyword(_text(message))
            if keyword == "skip":
                pending.append((ts, None))
            else:
                waiting.append(ts)
    if plan.issues:
        pending.extend((t, len(plan.issues) - 1) for t in waiting)
        plan.messages = sorted(pending, key=lambda item: float(item[0]))
    return plan


async def collect(
    slack: RebuildSlack,
    channel_id: str,
    bot: BotIdentity,
    *,
    oldest: str | None = None,
    repos: Collection[str] | Callable[[str], bool] | None = None,
    progress: Callable[[str], None] = lambda _: None,
    sleep: Sleep | None = None,
) -> ChannelPlan:
    """Read one channel and plan what to restore. Writes nothing.

    Every Slack call goes through ingest's :func:`with_backoff`, sharing one
    :class:`Budget` across the channel. When it runs out, ``SlackFetchError``
    stops the channel, and ``--merge`` continues later.
    """
    plan = ChannelPlan(channel_id)
    budget = Budget(CHANNEL_BUDGET_SECONDS)
    extra: dict[str, Any] = {} if sleep is None else {"sleep": sleep}

    def paced(
        fetch: Callable[[str | None], Awaitable[Mapping[str, Any]]], what: str
    ) -> Callable[[str | None], Awaitable[Mapping[str, Any]]]:
        async def call(cursor: str | None) -> Mapping[str, Any]:
            return await with_backoff(
                lambda: fetch(cursor), what=what, attempts=RETRY_ATTEMPTS, budget=budget, **extra
            )

        return call

    async def history(cursor: str | None) -> Mapping[str, Any]:
        return await slack.history(channel_id, cursor=cursor, oldest=oldest, limit=PAGE_SIZE)

    top = await _pages(paced(history, f"{channel_id}'s history"))
    plan.messages_read = len(top)
    for message in top:
        if _is_person(message, bot):
            parent = message.get("thread_ts")
            ts = str(message["ts"])
            plan.seen[ts] = parent if isinstance(parent, str) and parent != ts else None
    mode_ts = -1.0
    threads = [m for m in top if isinstance(m.get("reply_count"), int) and m["reply_count"] > 0]
    for message in top:
        if not _is_own(message, bot):
            continue
        text = _text(message)
        if _GUIDE in text:
            plan.guide_posted = True
        found = _MODE.search(text)
        if found is not None and float(str(message.get("ts") or 0)) > mode_ts:
            mode_ts = float(str(message.get("ts") or 0))
            plan.mode = ChannelMode(found["mode"])
    for n, parent in enumerate(threads, 1):
        ts = str(parent.get("ts") or "")
        replies = await _pages(paced(_replies(slack, channel_id, ts), f"a thread in {channel_id}"))
        plan.messages_read += max(len(replies) - 1, 0)
        for reply in replies:
            if _is_person(reply, bot) and str(reply["ts"]) != ts:
                plan.seen[str(reply["ts"])] = ts
        thread = plan_thread(ts, replies, bot, repos)
        plan.unknown_repos += thread.unknown_repos
        if thread.issues:
            plan.threads.append(thread)
        if n % 25 == 0 or n == len(threads):
            progress(f"{channel_id}: {n} of {len(threads)} threads read")
    return plan


def _replies(
    slack: RebuildSlack, channel_id: str, ts: str
) -> Callable[[str | None], Awaitable[Mapping[str, Any]]]:
    async def fetch(cursor: str | None) -> Mapping[str, Any]:
        return await slack.replies(channel_id, ts, cursor=cursor, limit=PAGE_SIZE)

    return fetch


def _allowed(repos: Collection[str] | Callable[[str], bool] | None) -> Callable[[str], bool] | None:
    if repos is None or callable(repos):
        return repos
    names = {r.casefold() for r in repos}
    return lambda repo: repo.casefold() in names


def _title(policy: Policy, channel_id: str, repo: str, number: int) -> str:
    label = repo.rsplit("/", 1)[-1]
    channel = policy.channels.get(channel_id)
    if channel is not None:
        for entry in channel.repos:
            if entry.repo.casefold() == repo.casefold():
                label = entry.nickname
    return f"{label}#{number}"[:80]


async def apply(store: Store, policy: Policy, plan: ChannelPlan, bot: BotIdentity) -> Restored:
    """Write a channel's plan. Adds what's missing and never overwrites what's there."""
    done = Restored()
    existing = {
        (t.topic.repo.casefold(), t.topic.issue_number): t.topic.id
        for t in await store.channel_topics(plan.channel_id)
        if t.topic.repo is not None and t.topic.issue_number is not None
    }
    for thread in plan.threads:
        topic_ids: list[str] = []
        for repo, number in thread.issues:
            key = (repo.casefold(), number)
            if key in existing:
                topic_ids.append(existing[key])
                done.reused_topics += 1
                continue
            topic = await store.create_topic(
                plan.channel_id, _title(policy, plan.channel_id, repo, number)
            )
            await store.set_topic_issue(topic.topic.id, repo, number)
            existing[key] = topic.topic.id
            topic_ids.append(topic.topic.id)
            done.topics += 1
        for ts, index in thread.messages:
            if await store.get_message(plan.channel_id, ts) is not None:
                done.kept += 1
                continue
            thread_ts = None if ts == thread.thread_ts else thread.thread_ts
            ref = MessageRef(channel_id=plan.channel_id, ts=ts, thread_ts=thread_ts)
            if index is None:
                await store.mark_chatter(ref, by="person")
                done.chatter += 1
            else:
                # The agent's, as the original link mostly was: a person's later choice,
                # or the agent's own later judgment, can still move it. A skip: above stays
                # a person's, since a person typed it.
                await store.link_message(ref, topic_ids[index], by="agent")
                done.linked += 1
    if plan.mode is not None and await store.get_channel_mode(plan.channel_id) is None:
        await store.set_channel_mode(plan.channel_id, plan.mode, changed_by=bot.user_id)
        done.mode = True
    if plan.guide_posted and await store.claim_notice(plan.channel_id, "", "guide"):
        done.guide = True
    log.info(
        "rebuilt %s: %d topics, %d reused, %d linked, %d chatter, %d kept",
        plan.channel_id,
        done.topics,
        done.reused_topics,
        done.linked,
        done.chatter,
        done.kept,
    )
    return done


# --- --from-github (#256) --------------------------------------------------------------------


@dataclass
class FromGithub:
    """Counts only, for the summary."""

    titles: int = 0
    topics: int = 0
    linked: int = 0
    not_ours: int = 0  # authored by anyone but JotFlow's write token: never trusted
    no_source: int = 0  # no source message the Slack walk saw in this channel


async def apply_github(
    store: Store,
    policy: Policy,
    plan: ChannelPlan,
    issues: Mapping[str, Sequence[MarkedIssue]],
    write_login: str,
) -> FromGithub:
    """Real titles, and threads whose Slack reply was lost, from JotFlow's own issues.

    ``issues`` maps each of the channel's repos to its marked issues. Only an
    issue authored by ``write_login`` counts, since anyone can write a marker
    line. A restored link is the agent's, so a person's later decision still
    wins, and only a message the Slack walk saw in this channel is linked.
    """
    done = FromGithub()
    bound = {
        (t.topic.repo.casefold(), t.topic.issue_number): t.topic
        for t in await store.channel_topics(plan.channel_id)
        if t.topic.repo is not None and t.topic.issue_number is not None
    }
    for repo, found in issues.items():
        for issue in found:
            if issue.author.casefold() != write_login.casefold():
                done.not_ours += 1
                continue
            topic = bound.get((repo.casefold(), issue.number))
            if topic is not None:
                # Only rebuild's own placeholder is replaced, never a title someone gave.
                if topic.title == _title(policy, plan.channel_id, repo, issue.number):
                    await store.rename_topic(topic.id, policy.issues.bare_title(issue.title))
                    done.titles += 1
                continue
            sources = [
                s for s in issue.sources if s.channel_id == plan.channel_id and s.ts in plan.seen
            ]
            if not sources:
                done.no_source += 1
                continue
            created = await store.create_topic(
                plan.channel_id, policy.issues.bare_title(issue.title)
            )
            await store.set_topic_issue(created.topic.id, repo, issue.number)
            bound[(repo.casefold(), issue.number)] = created.topic
            done.topics += 1
            for source in sources:
                linked = await store.get_message(plan.channel_id, source.ts)
                if linked is not None:
                    # One message cited by two issues fed two topics (#335): the second
                    # is an extra topic, never a move. A person's placement stays.
                    if linked.topic_id not in (None, created.topic.id) and (
                        linked.linked_by == "agent"
                    ):
                        with contextlib.suppress(ConflictError):
                            await store.also_link(linked.ref, created.topic.id)
                            done.linked += 1
                    continue
                ref = MessageRef(
                    channel_id=plan.channel_id, ts=source.ts, thread_ts=plan.seen[source.ts]
                )
                await store.link_message(ref, created.topic.id, by="agent")
                done.linked += 1
    log.info(
        "from GitHub in %s: %d titles, %d topics, %d linked, %d not ours, %d without a source",
        plan.channel_id,
        done.titles,
        done.topics,
        done.linked,
        done.not_ours,
        done.no_source,
    )
    return done
