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
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.models import TopicIndex, UntrustedText
from jotflow.orchestrator.ingest import NEW_MESSAGE_SUBTYPES, BotIdentity
from jotflow.orchestrator.keywords import split_keyword
from jotflow.store.base import Store
from jotflow.whiteboard.read_tools import Mention, RelatedExcerpt, ThreadMessage, WorkThread
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
EXCERPT_CHARS = 280
PAGE_SIZE = 200

RETRY_ATTEMPTS = 4
RETRY_BASE_SECONDS = 1.0
RETRY_CAP_SECONDS = 10.0
# Total waiting across retries stays well inside the MCP server's 30 s request timeout.
RETRY_BUDGET_SECONDS = 20.0

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


async def with_backoff[T](
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    sleep: Sleep = asyncio.sleep,
    attempts: int = RETRY_ATTEMPTS,
    budget: float = RETRY_BUDGET_SECONDS,
) -> T:
    """Run ``call``, retrying Slack rate limits with backoff.

    It waits ``Retry-After`` when Slack sends it, otherwise 1, 2, 4… seconds,
    each capped. It stops early when the next wait would take the total past
    ``budget``, so a slow retry never outlasts the request. Then it raises
    :class:`SlackFetchError`. Any other Slack error raises at once.
    """
    waited = 0.0
    for attempt in range(attempts):
        try:
            return await call()
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
            if waited + wait > budget:
                break
            waited += wait
            await sleep(wait)
    raise SlackFetchError(f"Slack is rate limiting {what}; try the run again shortly")


class SlackReader(Protocol):
    """The two Slack reads the packet needs. Tests use a fake."""

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]: ...

    async def user(self, user_id: str) -> Mapping[str, Any]: ...


class BoltSlackReader:
    """:class:`SlackReader` over slack-sdk's client, with the bot token's scopes."""

    def __init__(self, client: AsyncWebClient) -> None:
        self._client = client

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        response = await self._client.conversations_replies(
            channel=channel, ts=ts, cursor=cursor, limit=limit, inclusive=True
        )
        return response.data if isinstance(response.data, dict) else {}

    async def user(self, user_id: str) -> Mapping[str, Any]:
        response = await self._client.users_info(user=user_id)
        return response.data if isinstance(response.data, dict) else {}


# Slack markup to readable text.

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
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

    async def resolve(self, user_ids: Iterable[str]) -> dict[str, str]:
        names: dict[str, str] = {}
        for user_id in user_ids:
            if user_id not in names:
                names[user_id] = await self._one(user_id)
        return names

    async def _one(self, user_id: str) -> str:
        now = self.clock()
        cached = self._cache.get(user_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            data = await with_backoff(
                lambda: self.reader.user(user_id), what="users.info", sleep=self.sleep
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
    ) -> None:
        self._reader = reader
        self._store = store
        self._bot = bot
        self._sleep = sleep
        self._names = _Names(reader, sleep)

    async def _page(
        self, channel: str, ts: str, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        return await with_backoff(
            partial(self._reader.replies, channel, ts, cursor=cursor, limit=limit),
            what="conversations.replies",
            sleep=self._sleep,
        )

    async def _window(self, channel: str, thread_ts: str) -> tuple[list[Mapping[str, Any]], int]:
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
            page = await self._page(channel, thread_ts, cursor, PAGE_SIZE)
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

    async def _first(self, channel: str, thread_ts: str) -> Mapping[str, Any] | None:
        """A thread's first message only, for a related excerpt."""
        page = await self._page(channel, thread_ts, None, 1)
        messages = [m for m in page.get("messages") or [] if isinstance(m, Mapping)]
        return messages[0] if messages else None

    def _from_jotflow(self, raw: Mapping[str, Any]) -> bool:
        if self._bot is None:
            return False
        return raw.get("user") == self._bot.user_id or (
            self._bot.bot_id is not None and raw.get("bot_id") == self._bot.bot_id
        )

    async def _kept_by_person_as_chatter(self, channel: str, ts: str) -> tuple[bool, str | None]:
        link = await self._store.get_message(channel, ts)
        if link is None:
            return False, None
        return link.chatter and link.linked_by == "person", link.topic_id

    async def thread(self, channel_id: str, thread_ts: str) -> WorkThread:
        try:
            raw, skipped_while_paging = await self._window(channel_id, thread_ts)
        except SlackFetchError as error:
            raise ToolError(str(error)) from None
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
            author = message.get("user")
            if isinstance(author, str):
                ids.append(author)
            ids.extend(mentioned_ids(rest))
        names = await self._names.resolve(ids)

        messages = [self._message(m, rest, topic_id, names) for m, rest, topic_id in kept]
        trimmed, trimmed_out = _trim(messages)
        omitted = skipped_while_paging + trimmed_out
        log.info(
            "packet channel=%s thread=%s messages=%d omitted=%d",
            channel_id,
            thread_ts,
            len(trimmed),
            omitted,
        )
        return WorkThread(
            channel_id=channel_id, thread_ts=thread_ts, messages=trimmed, omitted=omitted
        )

    def _message(
        self,
        raw: Mapping[str, Any],
        rest: str,
        topic_id: str | None,
        names: Mapping[str, str],
    ) -> ThreadMessage:
        user_id = raw.get("user") if isinstance(raw.get("user"), str) else None
        if user_id is not None:
            author = names.get(user_id, user_id)
        else:
            profile = raw.get("bot_profile") or {}
            author = str(raw.get("username") or profile.get("name") or "a bot")
        keyword = split_keyword(str(raw.get("text") or ""))[0]
        return ThreadMessage(
            ts=str(raw["ts"]),
            user_id=user_id,
            author=UntrustedText(text=author),
            text=UntrustedText(text=_cut(readable(rest, names), MAX_MESSAGE_CHARS)),
            hint=keyword if keyword in ("idea", "bug", "re") else None,
            mentions=[
                Mention(user_id=uid, display_name=UntrustedText(text=names.get(uid, uid)))
                for uid in mentioned_ids(rest)
            ],
            topic_id=topic_id,
            from_jotflow=self._from_jotflow(raw),
        )

    async def related(self, thread: WorkThread, topics: TopicIndex) -> list[RelatedExcerpt]:
        # Rank the index's other threads by how many words their topic's title
        # shares with this thread, which is already fetched, so nothing is read
        # twice. No channel history: only the threads the index already names,
        # and only their first message.
        channel_id, thread_ts = thread.channel_id, thread.thread_ts
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
                first = await self._first(channel_id, other)
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
            names = await self._names.resolve(mentioned_ids(rest))
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
