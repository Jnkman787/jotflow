"""The work packet's Slack side (#20): the real ``WorkSource`` behind ``get_work``.

:class:`SlackWorkSource` fetches exactly one thread with ``conversations.replies``.
It never reads channel history. It turns Slack markup into readable text, and
resolves the author and every ``<@U…>`` mention to a display name, keeping the
user id alongside (#35). It strips a keyword prefix into a hint (D10), and drops
what must not reach the agent: ``skip:`` messages, a whole thread under a
``skip:`` root, and messages a person marked as chatter. Very long threads are
trimmed within fixed caps.

Text lives only in the packet this returns, for the one ``get_work`` call. The
source keeps no message text between calls; its only cache maps user ids to
display names, which are directory data. Log lines carry ids and counts only.

Every Slack call goes through :func:`with_backoff`, which retries rate limits
and honours ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, Protocol

import aiohttp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.change_requests import change_request_by
from jotflow.models import TopicIndex, UntrustedText
from jotflow.orchestrator.ingest import NEW_MESSAGE_SUBTYPES, BotIdentity
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.questions import mentions_bot
from jotflow.orchestrator.slack_files import (
    MAX_THREAD_READS,
    MAX_THREAD_TEXT,
    READ_LIMIT,
    TEXT_OFF,
    THREAD_FULL,
    FileSlack,
    is_text,
    message_files,
    read_text,
)
from jotflow.store.base import Store
from jotflow.whiteboard.read_tools import (
    FileBrief,
    FollowsPost,
    Mention,
    RelatedExcerpt,
    ThreadMessage,
    WorkThread,
)
from jotflow.whiteboard.tools import ToolError

log = logging.getLogger("jotflow.packet")

MAX_MESSAGES = 200  # newest messages kept while paging a very long thread
RAW_HEAD = 50  # oldest messages kept while paging, so the root and first replies survive
MAX_PAGES = 25  # conversations.replies pages read per thread, at most
MAX_MESSAGE_CHARS = 4_000
MAX_PACKET_CHARS = 60_000
KEEP_HEAD = 10  # replies kept after the root when trimming
KEEP_TAIL = 60  # most recent replies kept when trimming
MIN_TAIL = 5
MAX_RELATED = 3
# A top-level post this soon after the same author's previous one likely adds to it (#334).
FOLLOWS_WITHIN = 120
FOLLOWS_LOOKBACK = 20  # channel messages read, at most, to find it
EXCERPT_CHARS = 280
PAGE_SIZE = 200

RETRY_ATTEMPTS = 4
RETRY_BASE_SECONDS = 1.0
RETRY_CAP_SECONDS = 10.0
# Total waiting across retries stays well inside the MCP server's 30 s request timeout.
RETRY_BUDGET_SECONDS = 20.0

# Each Slack request's own timeout, so one slow answer cannot use up get_work's
# time. slack-sdk's default is 30 s, the whole MCP request timeout.
READER_TIMEOUT_SECONDS = 8

NAME_CACHE_SECONDS = 600.0
NAME_CACHE_SIZE = 1_000

Sleep = Callable[[float], Awaitable[None]]


class SlackFetchError(Exception):
    """Slack refused a read, or kept rate limiting it. The message says which."""


def _retry_after(error: SlackApiError) -> float | None:
    headers = getattr(error.response, "headers", None) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return None if value is None else max(0.0, float(value))
    except (TypeError, ValueError):
        return None


@dataclass
class Budget:
    """Seconds of rate-limit waiting that one piece of work may spend.

    The caller creates one budget and shares it across every Slack call that
    piece of work makes, so the total wait stays bounded however many calls it
    takes. ``get_work`` shares one across the thread's pages, each name lookup
    and the related excerpts, which keeps it inside the MCP server's 30 s
    request timeout. The auto-mode guest scan (#161) shares one across its
    member and directory pages.
    """

    remaining: float = RETRY_BUDGET_SECONDS

    def take(self, wait: float) -> bool:
        """Spend ``wait`` seconds if the budget allows it."""
        if wait > self.remaining:
            return False
        self.remaining -= wait
        return True


async def with_backoff[T](
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    sleep: Sleep = asyncio.sleep,
    attempts: int = RETRY_ATTEMPTS,
    budget: Budget | None = None,
) -> T:
    """Run ``call``, retrying Slack rate limits with backoff.

    It waits ``Retry-After`` when Slack sends it, otherwise 1, 2, 4… seconds,
    each capped. It stops when the next wait would overspend ``budget``, which
    is shared across calls, so a slow retry never outlasts the request. Then it
    raises :class:`SlackFetchError`. Any other Slack error raises at once, and so
    does a request that times out or loses its connection, without a retry, so
    the caller's fallbacks apply: a name falls back to the user id, a related
    excerpt is dropped, and an unreadable thread is a clean tool error.
    """
    budget = budget if budget is not None else Budget()
    for attempt in range(attempts):
        try:
            return await call()
        except (TimeoutError, aiohttp.ClientError):
            raise SlackFetchError(f"Slack did not answer {what}") from None
        except SlackApiError as error:
            status = getattr(error.response, "status_code", None)
            if status != 429:
                code = error.response.get("error", "unknown_error") if error.response else "?"
                raise SlackFetchError(f"Slack refused {what} ({code})") from None
            if attempt == attempts - 1:
                break
            wait = _retry_after(error)
            if wait is None:
                wait = RETRY_BASE_SECONDS * (2**attempt)
            wait = min(wait, RETRY_CAP_SECONDS)
            if not budget.take(wait):
                break
            await sleep(wait)
    raise SlackFetchError(f"Slack is rate limiting {what}; try the run again shortly")


class SlackReader(Protocol):
    """The two Slack reads the packet needs. Tests use a fake."""

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]: ...

    async def user(self, user_id: str) -> Mapping[str, Any]: ...

    async def history(
        self, channel: str, *, oldest: str, latest: str, limit: int
    ) -> Mapping[str, Any]: ...


class BoltSlackReader:
    """:class:`SlackReader` over slack-sdk's client, with the bot token's scopes."""

    def __init__(self, client: AsyncWebClient, *, timeout: int = READER_TIMEOUT_SECONDS) -> None:
        # A copy with a short per-request timeout, leaving the caller's client
        # alone. It keeps everything else, so a custom CA or proxy still applies.
        self._client = AsyncWebClient(
            token=client.token,
            base_url=client.base_url,
            timeout=timeout,
            ssl=client.ssl,
            proxy=client.proxy,
            trust_env_in_session=client.trust_env_in_session,
            headers=client.headers,
            team_id=client.default_params.get("team_id"),
            logger=client.logger,
            retry_handlers=client.retry_handlers,
        )

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        response = await self._client.conversations_replies(
            channel=channel,
            ts=ts,
            cursor=cursor,
            limit=limit,
            inclusive=True,
            include_all_metadata=True,
        )
        return response.data if isinstance(response.data, dict) else {}

    async def user(self, user_id: str) -> Mapping[str, Any]:
        response = await self._client.users_info(user=user_id)
        return response.data if isinstance(response.data, dict) else {}

    async def history(
        self, channel: str, *, oldest: str, latest: str, limit: int
    ) -> Mapping[str, Any]:
        # Top-level posts only, in a short window just before one post (#334).
        response = await self._client.conversations_history(
            channel=channel, oldest=oldest, latest=latest, limit=limit, inclusive=False
        )
        return response.data if isinstance(response.data, dict) else {}


# Slack markup to readable text.

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
# A Slack user id, as write_tools checks participants. Anything else is no author.
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")


def _author_id(raw: Mapping[str, Any]) -> str | None:
    user = raw.get("user")
    return user if isinstance(user, str) and _SLACK_USER.fullmatch(user) else None


_CHANNEL = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_SUBTEAM = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")
_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "have",
        "will",
        "would",
        "should",
        "could",
        "there",
        "their",
        "about",
        "what",
        "when",
        "which",
        "also",
        "just",
        "like",
        "into",
        "some",
        "more",
        "then",
        "them",
        "they",
        "your",
        "here",
        "only",
        "very",
    ]
)


def mentioned_ids(text: str) -> list[str]:
    """User ids mentioned in ``text``, in order, without repeats."""
    seen: list[str] = []
    for user_id in _MENTION.findall(text):
        if user_id not in seen:
            seen.append(user_id)
    return seen


def readable(text: str, names: Mapping[str, str]) -> str:
    """Slack's message markup as plain text, with mentions as display names."""
    text = _MENTION.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text)
    text = _CHANNEL.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = _SPECIAL.sub(lambda m: "@" + m.group(1), text)
    text = _SUBTEAM.sub(lambda m: m.group(1) or "@group", text)
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class _Names:
    """User id to display name, bounded and short-lived. No message text."""

    reader: SlackReader
    sleep: Sleep
    clock: Callable[[], float] = time.monotonic
    _cache: dict[str, tuple[str, float]] = field(default_factory=dict)

    async def resolve(
        self, user_ids: Iterable[str], budget: Budget | None = None
    ) -> dict[str, str]:
        """Names for ``user_ids``. A lookup the budget cannot afford falls back to the id."""
        shared = budget if budget is not None else Budget()
        names: dict[str, str] = {}
        for user_id in user_ids:
            if user_id not in names:
                names[user_id] = await self._one(user_id, shared)
        return names

    async def _one(self, user_id: str, budget: Budget) -> str:
        now = self.clock()
        cached = self._cache.get(user_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            data = await with_backoff(
                lambda: self.reader.user(user_id),
                what="users.info",
                sleep=self.sleep,
                budget=budget,
            )
        except SlackFetchError:
            return user_id  # a name is a convenience; the id still identifies them
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        if len(self._cache) >= NAME_CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[user_id] = (str(name), now + NAME_CACHE_SECONDS)
        return str(name)


class SlackWorkSource:
    """The live :class:`~jotflow.whiteboard.read_tools.WorkSource` (#20)."""

    def __init__(
        self,
        reader: SlackReader,
        store: Store,
        *,
        bot: BotIdentity | None = None,
        sleep: Sleep = asyncio.sleep,
        files: FileSlack | None = None,
        attachments_for: Callable[[str], str] = lambda channel_id: "names",
    ) -> None:
        self._reader = reader
        self._store = store
        self._bot = bot
        # Slack files (#333): what each channel shows, and JotFlow's own file reader.
        self._files = files
        self._attachments_for = attachments_for
        self._sleep = sleep
        self._names = _Names(reader, sleep)
        # One wait budget per get_work call, handed from thread() to related().
        # It holds only a number of seconds, never message text.
        self._budgets: dict[tuple[str, str], Budget] = {}

    async def _page(
        self, channel: str, ts: str, cursor: str | None, limit: int, budget: Budget
    ) -> Mapping[str, Any]:
        return await with_backoff(
            partial(self._reader.replies, channel, ts, cursor=cursor, limit=limit),
            what="conversations.replies",
            sleep=self._sleep,
            budget=budget,
        )

    async def _window(
        self, channel: str, thread_ts: str, budget: Budget
    ) -> tuple[list[Mapping[str, Any]], int]:
        """The thread's oldest and newest messages, oldest first, and how many were skipped.

        Pages through the whole thread, up to :data:`MAX_PAGES`, keeping the
        first :data:`RAW_HEAD` messages and a rolling window of the newest
        :data:`MAX_MESSAGES`, so a very long thread still shows its latest replies.
        """
        head: list[Mapping[str, Any]] = []
        tail: deque[Mapping[str, Any]] = deque(maxlen=MAX_MESSAGES)
        total = 0
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page = await self._page(channel, thread_ts, cursor, PAGE_SIZE, budget)
            for message in page.get("messages") or []:
                if not isinstance(message, Mapping):
                    continue
                total += 1
                (head if len(head) < RAW_HEAD else tail).append(message)
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not page.get("has_more") or not cursor:
                break
        kept = head + list(tail)
        return kept, total - len(kept)

    async def _first(
        self, channel: str, thread_ts: str, budget: Budget
    ) -> Mapping[str, Any] | None:
        """A thread's first message only, for a related excerpt."""
        page = await self._page(channel, thread_ts, None, 1, budget)
        messages = [m for m in page.get("messages") or [] if isinstance(m, Mapping)]
        return messages[0] if messages else None

    def _from_jotflow(self, raw: Mapping[str, Any]) -> bool:
        if self._bot is None:
            return False
        return raw.get("user") == self._bot.user_id or (
            self._bot.bot_id is not None and raw.get("bot_id") == self._bot.bot_id
        )

    async def _reaction_hint(self, channel: str, ts: str) -> Literal["idea", "bug"] | None:
        """The earliest idea or bug reaction on the message, if any (#139)."""
        for mark in await self._store.reaction_marks(channel, ts):
            if mark.keyword == "idea":
                return "idea"
            if mark.keyword == "bug":
                return "bug"
        return None

    async def _with_also(self, channel_id: str, message: ThreadMessage) -> ThreadMessage:
        """A message's other topics, when the agent split its actions (#335)."""
        if message.topic_id is None:
            return message
        link = await self._store.get_message(channel_id, message.ts)
        if link is None or not link.also_topics:
            return message
        return message.model_copy(update={"also_topics": list(link.also_topics)})

    async def _kept_by_person_as_chatter(self, channel: str, ts: str) -> tuple[bool, str | None]:
        link = await self._store.get_message(channel, ts)
        if link is None:
            return False, None
        return link.chatter and link.linked_by == "person", link.topic_id

    async def thread(self, channel_id: str, thread_ts: str) -> WorkThread:
        budget = Budget()
        try:
            raw, skipped_while_paging = await self._window(channel_id, thread_ts, budget)
        except SlackFetchError as error:
            # No packet: get_work records nothing when the thread cannot be read.
            raise ToolError(str(error)) from None
        # A thread under one of JotFlow's own top-level posts, a summary, the help guide,
        # a mode announcement or the live check's post, is never drafted (#318). This
        # holds however the run was armed, such as by undoing a skip reaction on a reply
        # there, whose thread Slack's reaction event doesn't name (#373).
        first = next((m for m in raw if isinstance(m.get("ts"), str)), None)
        if first is not None and self._from_jotflow(first):
            raise ToolError("this thread is under JotFlow's own post, so there is nothing to draft")
        root = next((m for m in raw if m.get("ts") == thread_ts), None)
        if root is not None and split_keyword(str(root.get("text") or ""))[0] == "skip":
            raise ToolError("this thread's first message says skip:, so there is nothing to draft")

        kept: list[tuple[Mapping[str, Any], str, str | None]] = []
        for message in raw:
            ts = message.get("ts")
            subtype = message.get("subtype")
            if not isinstance(ts, str) or (
                subtype not in NEW_MESSAGE_SUBTYPES and subtype != "bot_message"
            ):
                continue
            keyword, rest = split_keyword(str(message.get("text") or ""))
            if keyword == "skip":
                continue
            skipped, topic_id = await self._kept_by_person_as_chatter(channel_id, ts)
            if skipped:
                continue
            kept.append((message, rest, topic_id))

        ids: list[str] = []
        for message, rest, _ in kept:
            author = _author_id(message)
            if author is not None:
                ids.append(author)
            ids.extend(mentioned_ids(rest))
        names = await self._names.resolve(ids, budget)

        hints = {
            str(m["ts"]): await self._reaction_hint(channel_id, str(m["ts"])) for m, _, _ in kept
        }
        messages = [
            self._message(m, rest, topic_id, names, hints.get(str(m["ts"])))
            for m, rest, topic_id in kept
        ]
        messages = await self._with_files(channel_id, [m for m, _, _ in kept], messages)
        messages = [await self._with_also(channel_id, m) for m in messages]
        trimmed, trimmed_out = _trim(messages)
        omitted = skipped_while_paging + trimmed_out
        log.info(
            "packet channel=%s thread=%s messages=%d omitted=%d",
            channel_id,
            thread_ts,
            len(trimmed),
            omitted,
        )
        if len(self._budgets) >= NAME_CACHE_SIZE:  # pragma: no cover - one run per thread
            self._budgets.clear()
        self._budgets[(channel_id, thread_ts)] = budget
        follows = await self._follows(channel_id, root, budget) if root is not None else None
        return WorkThread(
            channel_id=channel_id,
            thread_ts=thread_ts,
            messages=trimmed,
            omitted=omitted,
            follows=follows,
        )

    async def _follows(
        self, channel: str, root: Mapping[str, Any], budget: Budget
    ) -> FollowsPost | None:
        """The author's own previous top-level post, if it came just before (#334).

        One read of the channel's few messages in the window before this post. A
        failure means no hint: the thread is drafted on its own, as before.
        """
        author, ts = _author_id(root), str(root.get("ts") or "")
        if author is None or self._from_jotflow(root) or root.get("bot_id"):
            return None
        try:
            posted = float(ts)
        except ValueError:
            return None
        oldest = f"{posted - FOLLOWS_WITHIN:.6f}"
        try:
            page = await with_backoff(
                partial(
                    self._reader.history,
                    channel,
                    oldest=oldest,
                    latest=ts,
                    limit=FOLLOWS_LOOKBACK,
                ),
                what="conversations.history",
                sleep=self._sleep,
                budget=budget,
            )
        except Exception as exc:  # a hint must never cost the packet
            log.info("no follows hint for channel=%s: %s", channel, type(exc).__name__)
            return None
        before = []
        for message in page.get("messages") or []:
            if not isinstance(message, Mapping) or _author_id(message) != author:
                continue
            other = str(message.get("ts") or "")
            parent = message.get("thread_ts")
            if parent not in (None, other) or message.get("bot_id"):
                continue  # a reply in some thread, not a top-level post
            if message.get("subtype") not in NEW_MESSAGE_SUBTYPES:
                continue
            try:
                gap = posted - float(other)
            except ValueError:
                continue
            if 0 < gap <= FOLLOWS_WITHIN:
                before.append((gap, other))
        if not before:
            return None
        gap, other = min(before)  # the nearest one
        skipped, topic_id = await self._kept_by_person_as_chatter(channel, other)
        if skipped:
            return None  # a person said skip: for it
        return FollowsPost(ts=other, seconds_before=int(gap), topic_id=topic_id)

    def _message(
        self,
        raw: Mapping[str, Any],
        rest: str,
        topic_id: str | None,
        names: Mapping[str, str],
        reaction_hint: Literal["idea", "bug"] | None = None,
    ) -> ThreadMessage:
        user_id = _author_id(raw)
        if user_id is not None:
            author = names.get(user_id, user_id)
        elif isinstance(raw.get("user"), str):
            author = "someone"  # not a Slack user id, so neither it nor a name is kept
        else:
            profile = raw.get("bot_profile") or {}
            author = str(raw.get("username") or profile.get("name") or "a bot")
        keyword = split_keyword(str(raw.get("text") or ""))[0]
        return ThreadMessage(
            ts=str(raw["ts"]),
            user_id=user_id,
            author=UntrustedText(text=author),
            text=UntrustedText(text=_cut(readable(rest, names), MAX_MESSAGE_CHARS)),
            # A keyword in the text wins over a reaction on the message (#139).
            hint=keyword if keyword in ("idea", "bug", "re", "ask") else reaction_hint,
            mentions=[
                Mention(user_id=uid, display_name=UntrustedText(text=names.get(uid, uid)))
                for uid in mentioned_ids(rest)
            ],
            topic_id=topic_id,
            from_jotflow=self._from_jotflow(raw),
            asks_jotflow=self._asks_jotflow(raw, keyword),
            change_request_by=change_request_by(
                raw, self._bot.user_id if self._bot is not None else None
            ),
        )

    async def _with_files(
        self, channel_id: str, raw: list[Mapping[str, Any]], messages: list[ThreadMessage]
    ) -> list[ThreadMessage]:
        """Each message's files, as the channel's ``attachments`` setting allows (#333).

        Never a URL. With ``text``, JotFlow reads a small text file itself, up to
        :data:`MAX_THREAD_TEXT` for the thread, and gives its text as untrusted.
        """
        setting = self._attachments_for(channel_id)
        if setting == "off":
            return messages
        used = 0
        reads = 0
        out: list[ThreadMessage] = []
        for source, message in zip(raw, messages, strict=True):
            found = [] if self._from_jotflow(source) else message_files(source)
            briefs: list[FileBrief] = []
            for file in found:
                text: str | None = None
                reason: str | None = None
                read_sha: str | None = None
                if is_text(file):
                    if setting != "text" or self._files is None:
                        reason = TEXT_OFF
                    elif used + file.size > MAX_THREAD_TEXT:
                        reason = THREAD_FULL
                    elif reads >= MAX_THREAD_READS:
                        reason = READ_LIMIT
                    else:
                        reads += 1
                        read = await read_text(file, self._files)
                        text, reason, read_sha = read.text, read.reason, read.sha256
                        if text is not None:
                            used += len(text.encode())
                briefs.append(
                    FileBrief(
                        file_id=file.file_id,
                        name=UntrustedText(text=file.name),
                        filetype=file.filetype,
                        mimetype=file.mimetype,
                        size=file.size,
                        text=UntrustedText(text=text) if text is not None else None,
                        not_read=reason,
                        sha256=read_sha,
                    )
                )
            out.append(message.model_copy(update={"files": briefs}) if briefs else message)
        return out

    def _asks_jotflow(self, raw: Mapping[str, Any], keyword: str | None) -> bool:
        """A person asked JotFlow a question: ``ask:``, or a mention of the bot (#57)."""
        if self._from_jotflow(raw) or raw.get("bot_id"):
            return False
        if keyword == "ask":
            return True
        return self._bot is not None and mentions_bot(str(raw.get("text") or ""), self._bot.user_id)

    async def related(self, thread: WorkThread, topics: TopicIndex) -> list[RelatedExcerpt]:
        # Rank the index's other threads by how many words their topic's title
        # shares with this thread, which is already fetched, so nothing is read
        # twice. No channel history: only the threads the index already names,
        # and only their first message.
        channel_id, thread_ts = thread.channel_id, thread.thread_ts
        # The rest of this get_work call's wait budget, after the thread's fetch.
        budget = self._budgets.pop((channel_id, thread_ts), None) or Budget()
        own_words = _words(" ".join(m.text.text for m in thread.messages))
        ranked: list[tuple[int, str, str]] = []
        seen = {thread_ts}
        for summary in topics.topics:
            score = len(own_words & _words(summary.title.text))
            for other in summary.threads:
                if other not in seen and score > 0:
                    seen.add(other)
                    ranked.append((score, other, summary.id))
        ranked.sort(key=lambda r: -r[0])

        excerpts: list[RelatedExcerpt] = []
        for _, other, topic_id in ranked:
            if len(excerpts) >= MAX_RELATED:
                break
            try:
                first = await self._first(channel_id, other, budget)
            except SlackFetchError:
                continue
            if first is None:
                continue
            ts = first.get("ts")
            keyword, rest = split_keyword(str(first.get("text") or ""))
            if not isinstance(ts, str) or keyword == "skip":
                continue
            if (await self._kept_by_person_as_chatter(channel_id, ts))[0]:
                continue
            names = await self._names.resolve(mentioned_ids(rest), budget)
            excerpts.append(
                RelatedExcerpt(
                    ts=ts,
                    thread_ts=other,
                    topic_id=topic_id,
                    excerpt=UntrustedText(text=_cut(readable(rest, names), EXCERPT_CHARS)),
                )
            )
        return excerpts


def _trim(messages: list[ThreadMessage]) -> tuple[list[ThreadMessage], int]:
    """Keep the root, the first replies, and the most recent ones, within the caps.

    The tail shrinks first, down to :data:`MIN_TAIL`, then the head, down to the
    root alone, so a packet of long messages still fits :data:`MAX_PACKET_CHARS`.
    """

    def size(kept: list[ThreadMessage]) -> int:
        return sum(len(m.text.text) + len(m.author.text) for m in kept)

    if len(messages) <= 1 + KEEP_HEAD + KEEP_TAIL and size(messages) <= MAX_PACKET_CHARS:
        return messages, 0
    head_count = min(1 + KEEP_HEAD, len(messages))
    tail_count = min(KEEP_TAIL, len(messages) - head_count)
    while True:
        kept = messages[:head_count] + (
            messages[len(messages) - tail_count :] if tail_count else []
        )
        if size(kept) <= MAX_PACKET_CHARS:
            return kept, len(messages) - len(kept)
        if tail_count > MIN_TAIL:
            tail_count -= 1
        elif head_count > 1:
            head_count -= 1
        else:
            # The root and the minimum tail alone; each is capped at MAX_MESSAGE_CHARS.
            return kept, len(messages) - len(kept)
