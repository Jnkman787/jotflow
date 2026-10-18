"""The live work packet (#20), against a fake Slack reader. No network."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.models import MessageRef, RunKind, TopicIndex, TopicSummary, UntrustedText
from jotflow.orchestrator import packet
from jotflow.orchestrator.ingest import BotIdentity
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.packet import (
    SlackFetchError,
    SlackWorkSource,
    _Names,
    mentioned_ids,
    readable,
    with_backoff,
)
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore, SqliteStore
from jotflow.whiteboard.read_tools import NoArguments, read_tools
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError, checked_registry

CH = "C0IDEAS01"
ROOT = "1760781600.000100"
SECRET = "the launch plan is swordfish"


class FakeResponse:
    def __init__(self, status: int, error: str, retry_after: str | None = None) -> None:
        self.status_code = status
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}
        self.data = {"ok": False, "error": error}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def slack_error(status: int, error: str, retry_after: str | None = None) -> SlackApiError:
    return SlackApiError(error, FakeResponse(status, error, retry_after))


def msg(ts: str, text: str, user: str = "U0SAM", **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "text": text, "user": user, "thread_ts": ROOT, **extra}


class FakeReader:
    """Threads by ts, users by id, scripted failures, and a log of every call."""

    def __init__(self, threads: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.threads = threads or {}
        self.users = {
            "U0SAM": {"profile": {"display_name": "Sam"}},
            "U0ALEX": {"profile": {"display_name": "", "real_name": "Alex Rivera"}},
            "U0BOT": {"profile": {"display_name": "Idea Bot"}},
        }
        self.calls: list[tuple[str, str]] = []
        self.failures: list[SlackApiError] = []
        self.page_size: int | None = None

    def _fail(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        self.calls.append(("replies", ts))
        self._fail()
        messages = self.threads.get(ts, [])
        size = self.page_size or limit
        start = int(cursor or 0)
        page = messages[start : start + size]
        more = start + size < len(messages)
        return {
            "messages": page,
            "has_more": more,
            "response_metadata": {"next_cursor": str(start + size) if more else ""},
        }

    async def user(self, user_id: str) -> Mapping[str, Any]:
        self.calls.append(("user", user_id))
        if user_id not in self.users:
            raise slack_error(404, "user_not_found")
        return {"user": self.users[user_id]}


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def source(reader: FakeReader, store: Any = None, **kw: Any) -> SlackWorkSource:
    return SlackWorkSource(reader, store or MemoryStore(), sleep=Sleeps(), **kw)


async def related_to_root(src: SlackWorkSource, topics: TopicIndex) -> list[Any]:
    """Fetch the root thread, as get_work does, then rank related threads against it."""
    return await src.related(await src.thread(CH, ROOT), topics)


def index(*summaries: TopicSummary) -> TopicIndex:
    return TopicIndex(
        channel_id=CH,
        window_days=14,
        generated_at=datetime(2026, 10, 18, tzinfo=UTC),
        topics=list(summaries),
    )


def summary(topic_id: str, title: str, threads: list[str]) -> TopicSummary:
    return TopicSummary(
        id=topic_id,
        title=UntrustedText(text=title),
        message_count=1,
        threads=threads,
        last_active=datetime(2026, 10, 18, tzinfo=UTC),
    )


# Keywords


@pytest.mark.parametrize(
    ("text", "keyword", "rest"),
    [
        ("idea: a progress bar", "idea", "a progress bar"),
        ("BUG:login fails", "bug", "login fails"),
        ("  re: web#42", "re", "web#42"),
        ("skip: not an idea", "skip", "not an idea"),
        ("Skip button is broken", None, "Skip button is broken"),
        ("ideas: plural is text", None, "ideas: plural is text"),
        ("a bug: in the middle", None, "a bug: in the middle"),
    ],
)
def test_the_strict_keyword_prefix(text: str, keyword: str | None, rest: str) -> None:
    assert split_keyword(text) == (keyword, rest)


# Markup


def test_markup_reads_as_plain_text() -> None:
    names = {"U0ALEX": "Alex"}
    text = (
        "<@U0ALEX|alex> see <#C0OTHER|design> and <https://acme.example/x|the doc>, "
        "<https://acme.example/y>, <!here>, <!subteam^S01|@web-team>, a &amp; b &lt;3"
    )
    assert readable(text, names) == (
        "@Alex see #design and the doc (https://acme.example/x), "
        "https://acme.example/y, @here, @web-team, a & b <3"
    )
    assert readable("<@U0NOBODY> hi", {}) == "@U0NOBODY hi"


def test_mentions_in_order_without_repeats() -> None:
    assert mentioned_ids("<@U0A> <@U0B|b> <@U0A>") == ["U0A", "U0B"]


# The thread


async def test_only_the_thread_is_read_never_channel_history() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "idea: a progress bar"), msg("1.2", "Agreed")]})
    thread = await source(reader).thread(CH, ROOT)
    assert [m.ts for m in thread.messages] == [ROOT, "1.2"]
    assert {call for call, _ in reader.calls} == {"replies", "user"}
    assert [ts for call, ts in reader.calls if call == "replies"] == [ROOT]


async def test_keywords_become_hints_and_leave_the_text() -> None:
    reader = FakeReader(
        {ROOT: [msg(ROOT, "idea: a progress bar"), msg("1.2", "Skip button is broken")]}
    )
    first, second = (await source(reader).thread(CH, ROOT)).messages
    assert (first.hint, first.text.text) == ("idea", "a progress bar")
    assert (second.hint, second.text.text) == (None, "Skip button is broken")


async def test_skip_messages_are_left_out() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "a progress bar"), msg("1.2", f"skip: {SECRET}")]})
    thread = await source(reader).thread(CH, ROOT)
    assert [m.ts for m in thread.messages] == [ROOT]
    assert SECRET not in thread.model_dump_json()


async def test_a_skip_root_skips_the_whole_thread() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "skip: just chatting"), msg("1.2", "ok")]})
    with pytest.raises(ToolError, match="says skip:"):
        await source(reader).thread(CH, ROOT)


async def test_a_persons_chatter_mark_leaves_a_message_out() -> None:
    store = MemoryStore()
    reader = FakeReader({ROOT: [msg(ROOT, "a progress bar"), msg("1.2", SECRET), msg("1.3", "x")]})
    await store.mark_chatter(MessageRef(channel_id=CH, ts="1.2", thread_ts=ROOT), by="person")
    await store.mark_chatter(MessageRef(channel_id=CH, ts="1.3", thread_ts=ROOT), by="agent")
    thread = await source(reader, store).thread(CH, ROOT)
    assert [m.ts for m in thread.messages] == [ROOT, "1.3"]


async def test_existing_topic_links_are_shown_per_message() -> None:
    store = MemoryStore()
    one = await store.create_topic(CH, "Progress bar")
    two = await store.create_topic(CH, "Dark mode")
    await store.link_message(MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT), one.topic.id)
    await store.link_message(MessageRef(channel_id=CH, ts="1.2", thread_ts=ROOT), two.topic.id)
    reader = FakeReader({ROOT: [msg(ROOT, "progress bar"), msg("1.2", "and dark mode")]})
    thread = await source(reader, store).thread(CH, ROOT)
    assert [m.topic_id for m in thread.messages] == [one.topic.id, two.topic.id]


async def test_authors_and_mentions_are_names_with_ids_kept() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "<@U0ALEX> should own this", user="U0SAM")]})
    [message] = (await source(reader).thread(CH, ROOT)).messages
    assert (message.user_id, message.author.text) == ("U0SAM", "Sam")
    assert message.text.text == "@Alex Rivera should own this"
    assert [(m.user_id, m.display_name.text) for m in message.mentions] == [
        ("U0ALEX", "Alex Rivera")
    ]


async def test_an_unknown_user_keeps_their_id() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "hi <@U0GONE>", user="U0GONE")]})
    [message] = (await source(reader).thread(CH, ROOT)).messages
    assert message.author.text == "U0GONE"
    assert message.text.text == "hi @U0GONE"


async def test_names_are_looked_up_once_per_person() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "a"), msg("1.2", "b"), msg("1.3", "<@U0SAM>")]})
    await source(reader).thread(CH, ROOT)
    assert [ts for call, ts in reader.calls if call == "user"] == ["U0SAM"]


async def test_the_name_cache_expires() -> None:
    reader = FakeReader()
    now = [0.0]
    names = _Names(reader, Sleeps(), clock=lambda: now[0])
    await names.resolve(["U0SAM"])
    await names.resolve(["U0SAM"])
    now[0] += packet.NAME_CACHE_SECONDS + 1
    await names.resolve(["U0SAM"])
    assert [ts for call, ts in reader.calls if call == "user"] == ["U0SAM", "U0SAM"]


async def test_the_name_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packet, "NAME_CACHE_SIZE", 2)
    reader = FakeReader()
    reader.users.update({"U0C": {"name": "c"}})
    names = _Names(reader, Sleeps())
    await names.resolve(["U0SAM", "U0ALEX", "U0C"])
    assert len(names._cache) == 2
    # Only names, never message text.
    assert all(isinstance(name, str) for name, _ in names._cache.values())


async def test_jotflows_own_messages_are_marked_and_other_bots_kept() -> None:
    reader = FakeReader(
        {
            ROOT: [
                msg(ROOT, "a progress bar"),
                msg("1.2", "Which repo?", user="U0BOT", bot_id="B0BOT"),
                {
                    "ts": "1.3",
                    "text": "build passed",
                    "subtype": "bot_message",
                    "bot_id": "B0CI",
                    "username": "CI",
                },
                msg("1.4", "joined", subtype="channel_join"),
            ]
        }
    )
    bot = BotIdentity(user_id="U0BOT", bot_id="B0BOT")
    messages = (await source(reader, bot=bot).thread(CH, ROOT)).messages
    assert [(m.ts, m.from_jotflow, m.author.text) for m in messages] == [
        (ROOT, False, "Sam"),
        ("1.2", True, "Idea Bot"),
        ("1.3", False, "CI"),
    ]


async def test_long_threads_page_through_replies() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "root"), *(msg(f"1.{i:03}", f"r{i}") for i in range(5))]})
    reader.page_size = 2
    thread = await source(reader).thread(CH, ROOT)
    assert len(thread.messages) == 6
    assert len([c for c in reader.calls if c[0] == "replies"]) == 3


async def test_a_very_long_thread_keeps_the_root_the_start_and_the_end() -> None:
    replies = [msg(f"1.{i:04}", f"reply {i}") for i in range(100)]
    reader = FakeReader({ROOT: [msg(ROOT, "root"), *replies]})
    thread = await source(reader).thread(CH, ROOT)
    kept = [m.ts for m in thread.messages]
    assert kept[0] == ROOT
    assert kept[1 : 1 + packet.KEEP_HEAD] == [f"1.{i:04}" for i in range(packet.KEEP_HEAD)]
    assert kept[-1] == "1.0099"
    assert len(kept) == 1 + packet.KEEP_HEAD + packet.KEEP_TAIL
    assert thread.omitted == 101 - len(kept)


async def test_a_huge_message_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "x" * (packet.MAX_MESSAGE_CHARS + 500))]})
    [message] = (await source(reader).thread(CH, ROOT)).messages
    assert len(message.text.text) == packet.MAX_MESSAGE_CHARS
    assert message.text.text.endswith("…")


async def test_the_packet_size_cap_trims_more_of_the_middle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(packet, "MAX_PACKET_CHARS", 3_000)
    replies = [msg(f"1.{i:04}", "y" * 100) for i in range(80)]
    reader = FakeReader({ROOT: [msg(ROOT, "root"), *replies]})
    thread = await source(reader).thread(CH, ROOT)
    assert sum(len(m.text.text) + len(m.author.text) for m in thread.messages) <= 3_000
    assert thread.messages[0].ts == ROOT and thread.messages[-1].ts == "1.0079"
    assert thread.omitted > 0


# Rate limits


async def test_a_rate_limit_waits_retry_after_then_succeeds() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "root")]})
    reader.failures = [slack_error(429, "ratelimited", retry_after="3")]
    sleeps = Sleeps()
    thread = await SlackWorkSource(reader, MemoryStore(), sleep=sleeps).thread(CH, ROOT)
    assert len(thread.messages) == 1
    assert sleeps.waits == [3.0]


async def test_without_retry_after_the_backoff_doubles() -> None:
    calls = [0]

    async def flaky() -> str:
        calls[0] += 1
        if calls[0] < 3:
            raise slack_error(429, "ratelimited")
        return "ok"

    sleeps = Sleeps()
    assert await with_backoff(flaky, what="x", sleep=sleeps) == "ok"
    assert sleeps.waits == [1.0, 2.0]


async def test_a_long_retry_after_is_capped() -> None:
    calls = [0]

    async def slow() -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise slack_error(429, "ratelimited", retry_after="600")
        return "ok"

    sleeps = Sleeps()
    await with_backoff(slow, what="x", sleep=sleeps)
    assert sleeps.waits == [packet.RETRY_CAP_SECONDS]


async def test_endless_rate_limits_fail_clearly() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "root")]})
    reader.failures = [slack_error(429, "ratelimited")] * 10
    with pytest.raises(ToolError, match=r"rate limiting conversations\.replies"):
        await source(reader).thread(CH, ROOT)


async def test_other_slack_errors_fail_at_once() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "root")]})
    reader.failures = [slack_error(200, "thread_not_found")]
    with pytest.raises(ToolError, match=r"refused conversations.replies \(thread_not_found\)"):
        await source(reader).thread(CH, ROOT)


async def test_a_bad_retry_after_header_falls_back_to_backoff() -> None:
    calls = [0]

    async def odd() -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise slack_error(429, "ratelimited", retry_after="soon")
        return "ok"

    sleeps = Sleeps()
    await with_backoff(odd, what="x", sleep=sleeps)
    assert sleeps.waits == [1.0]


async def test_with_backoff_raises_fetch_errors() -> None:
    async def refused() -> str:
        raise slack_error(403, "missing_scope")

    with pytest.raises(SlackFetchError, match="missing_scope"):
        await with_backoff(refused, what="users.info", sleep=Sleeps())


# Related excerpts


async def test_related_threads_are_ranked_by_shared_title_words() -> None:
    reader = FakeReader(
        {
            ROOT: [msg(ROOT, "The onboarding progress bar is confusing")],
            "2.0": [msg("2.0", "Progress bar for onboarding steps", thread_ts="2.0")],
            "3.0": [msg("3.0", "Onboarding emails", thread_ts="3.0")],
            "4.0": [msg("4.0", "Dark mode", thread_ts="4.0")],
        }
    )
    topics = index(
        summary("t3", "Onboarding emails", ["3.0"]),
        summary("t2", "Onboarding progress bar", ["2.0"]),
        summary("t4", "Dark mode", ["4.0"]),
        summary("t1", "This thread", [ROOT]),
    )
    excerpts = await related_to_root(source(reader), topics)
    assert [(e.thread_ts, e.topic_id) for e in excerpts] == [("2.0", "t2"), ("3.0", "t3")]
    assert excerpts[0].excerpt.text == "Progress bar for onboarding steps"
    assert excerpts[0].excerpt.untrusted is True


async def test_related_excerpts_are_capped_and_cut() -> None:
    threads = {ROOT: [msg(ROOT, "progress bar")]}
    summaries = []
    for i in range(6):
        ts = f"{i + 2}.0"
        threads[ts] = [msg(ts, "progress bar " + "z" * 600, thread_ts=ts)]
        summaries.append(summary(f"t{i}", "Progress bar", [ts]))
    excerpts = await related_to_root(source(FakeReader(threads)), index(*summaries))
    assert len(excerpts) == packet.MAX_RELATED
    assert all(len(e.excerpt.text) == packet.EXCERPT_CHARS for e in excerpts)


async def test_related_leaves_out_skipped_chatter_and_failures() -> None:
    store = MemoryStore()
    await store.mark_chatter(MessageRef(channel_id=CH, ts="3.0", thread_ts="3.0"), by="person")
    reader = FakeReader(
        {
            ROOT: [msg(ROOT, "progress bar")],
            "2.0": [msg("2.0", "skip: progress bar joke", thread_ts="2.0")],
            "3.0": [msg("3.0", "progress bar", thread_ts="3.0")],
            "5.0": [msg("5.0", "progress bar <@U0ALEX>", thread_ts="5.0")],
        }
    )
    topics = index(summary("t", "Progress bar", ["2.0", "3.0", "4.0", "5.0"]))
    excerpts = await related_to_root(source(reader, store), topics)
    assert [e.thread_ts for e in excerpts] == ["5.0"]
    assert excerpts[0].excerpt.text == "progress bar @Alex Rivera"


async def test_related_ranks_from_the_fetched_thread_without_reading_it_again() -> None:
    reader = FakeReader(
        {
            ROOT: [msg(ROOT, "The onboarding progress bar")],
            "2.0": [msg("2.0", "Progress bar ideas", thread_ts="2.0")],
        }
    )
    src = source(reader)
    thread = await src.thread(CH, ROOT)
    before = [ts for call, ts in reader.calls if call == "replies"]
    await src.related(thread, index(summary("t", "Progress bar", ["2.0"])))
    after = [ts for call, ts in reader.calls if call == "replies"]
    assert before == [ROOT]
    assert after == [ROOT, "2.0"]  # no second read of the run's own thread


async def test_a_related_thread_that_fails_is_skipped() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "progress bar")]})

    original = reader.replies

    async def failing_for_others(
        channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        if ts != ROOT:
            raise slack_error(200, "thread_not_found")
        return await original(channel, ts, cursor=cursor, limit=limit)

    reader.replies = failing_for_others  # type: ignore[method-assign]
    topics = index(summary("t", "Progress bar", ["2.0"]))
    assert await related_to_root(source(reader), topics) == []


# Nothing persists


async def test_message_text_is_never_stored(tmp_path: Path) -> None:
    path = tmp_path / "jotflow.db"
    store = await SqliteStore.open(path)
    try:
        policy = parse_policy(
            f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"
"""
        )
        reader = FakeReader({ROOT: [msg(ROOT, f"progress bar {SECRET}"), msg("1.2", SECRET)]})
        run = await store.enqueue_run(RunKind.DRAFTING, CH, ROOT)
        claimed = await store.claim_next_run(max_running=1)
        assert claimed is not None and claimed.id == run.id
        tools = checked_registry(read_tools(store, policy, source(reader, store)))
        grant = Grant(run.id, RunKind.DRAFTING, CH, thread_ts=ROOT)
        work = await tools["get_work"].handler(grant, NoArguments())
        assert SECRET in work.model_dump_json()  # served to the agent...
    finally:
        await store.close()
    stored = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert SECRET.encode() not in stored  # ...but never written down


# The real adapter, over a fake slack-sdk client


class FakeSlackResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeWebClient:
    def __init__(self, data: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.data = data if data is not None else {"ok": True, "messages": []}

    async def conversations_replies(self, **kwargs: Any) -> FakeSlackResponse:
        self.calls.append(("conversations_replies", kwargs))
        return FakeSlackResponse(self.data)

    async def users_info(self, **kwargs: Any) -> FakeSlackResponse:
        self.calls.append(("users_info", kwargs))
        return FakeSlackResponse(self.data)


async def test_the_bolt_reader_asks_for_one_thread_and_one_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeWebClient()
    reader = packet.BoltSlackReader(AsyncWebClient(token="xoxb-test"))
    monkeypatch.setattr(reader, "_client", client)
    assert await reader.replies(CH, ROOT, cursor="c1", limit=50) == client.data
    assert await reader.user("U0SAM") == client.data
    assert client.calls == [
        (
            "conversations_replies",
            {"channel": CH, "ts": ROOT, "cursor": "c1", "limit": 50, "inclusive": True},
        ),
        ("users_info", {"user": "U0SAM"}),
    ]


def test_the_bolt_reader_uses_a_short_request_timeout() -> None:
    original = AsyncWebClient(token="xoxb-test", timeout=30)
    reader = packet.BoltSlackReader(original)
    assert reader._client.timeout == packet.READER_TIMEOUT_SECONDS
    assert reader._client.token == "xoxb-test"
    assert original.timeout == 30  # the caller's client is left alone


async def test_the_bolt_reader_tolerates_a_non_dict_body() -> None:
    reader = packet.BoltSlackReader(AsyncWebClient(token="xoxb-test"))
    reader._client = FakeWebClient(data=b"raw")  # type: ignore[assignment]
    assert await reader.replies(CH, ROOT, cursor=None, limit=1) == {}
    assert await reader.user("U0SAM") == {}


# CodeRabbit's findings: the newest replies survive, and backoff fits the request


async def test_a_very_long_thread_still_shows_its_newest_replies() -> None:
    replies = [msg(f"1.{i:04}", f"reply {i}") for i in range(600)]
    reader = FakeReader({ROOT: [msg(ROOT, "root"), *replies]})
    thread = await source(reader).thread(CH, ROOT)
    kept = [m.ts for m in thread.messages]
    assert kept[0] == ROOT
    assert kept[-1] == "1.0599"  # the newest reply, past the first 200
    assert kept[1 : 1 + packet.KEEP_HEAD] == [f"1.{i:04}" for i in range(packet.KEEP_HEAD)]
    assert thread.omitted == 601 - len(kept)


async def test_paging_stops_at_the_page_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packet, "MAX_PAGES", 2)
    reader = FakeReader({ROOT: [msg(ROOT, "root"), *(msg(f"1.{i:03}", "r") for i in range(9))]})
    reader.page_size = 2
    await source(reader).thread(CH, ROOT)
    assert len([c for c in reader.calls if c[0] == "replies"]) == 2


async def test_related_reads_only_each_threads_first_message() -> None:
    reader = FakeReader(
        {
            ROOT: [msg(ROOT, "progress bar")],
            "2.0": [msg("2.0", "progress bar", thread_ts="2.0"), msg("2.1", "more")],
        }
    )
    await related_to_root(source(reader), index(summary("t", "Progress bar", ["2.0"])))
    assert [ts for call, ts in reader.calls if call == "replies"].count("2.0") == 1


async def test_an_empty_related_thread_is_skipped() -> None:
    reader = FakeReader({ROOT: [msg(ROOT, "progress bar")], "2.0": []})
    topics = index(summary("t", "Progress bar", ["2.0"]))
    assert await related_to_root(source(reader), topics) == []


async def test_backoff_stops_before_the_wait_budget_runs_out() -> None:
    async def always_limited() -> str:
        raise slack_error(429, "ratelimited", retry_after="8")

    sleeps = Sleeps()
    with pytest.raises(SlackFetchError, match="rate limiting"):
        await with_backoff(always_limited, what="x", sleep=sleeps, attempts=10)
    assert sleeps.waits == [8.0, 8.0]
    assert sum(sleeps.waits) <= packet.RETRY_BUDGET_SECONDS


async def test_long_early_replies_shrink_the_head_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packet, "MAX_PACKET_CHARS", 9_000)
    early = [msg(f"1.{i:04}", "h" * 4_000) for i in range(20)]
    late = [msg(f"2.{i:04}", "short") for i in range(80)]
    reader = FakeReader({ROOT: [msg(ROOT, "root"), *early, *late]})
    thread = await source(reader).thread(CH, ROOT)
    size = sum(len(m.text.text) + len(m.author.text) for m in thread.messages)
    assert size <= 9_000
    assert thread.messages[0].ts == ROOT and thread.messages[-1].ts == "2.0079"
    assert len([m for m in thread.messages if m.ts.startswith("1.")]) < packet.KEEP_HEAD


async def test_a_tiny_cap_still_keeps_the_root_and_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(packet, "MAX_PACKET_CHARS", 50)
    replies = [msg(f"1.{i:04}", "z" * 40) for i in range(90)]
    reader = FakeReader({ROOT: [msg(ROOT, "r" * 40), *replies]})
    thread = await source(reader).thread(CH, ROOT)
    assert thread.messages[0].ts == ROOT
    assert len(thread.messages) == 1 + packet.MIN_TAIL
    assert thread.messages[-1].ts == "1.0089"


# One wait budget per get_work call, shared by every Slack call


class RateLimitedNames(FakeReader):
    """users.info is always rate limited, asking for 8 s."""

    async def user(self, user_id: str) -> Mapping[str, Any]:
        self.calls.append(("user", user_id))
        raise slack_error(429, "ratelimited", retry_after="8")


async def test_a_rate_limited_thread_with_many_mentions_stays_within_one_budget() -> None:
    mentions = " ".join(f"<@U0P{i}>" for i in range(5))
    reader = RateLimitedNames({ROOT: [msg(ROOT, f"who owns this? {mentions}", user="U0NEW")]})
    reader.failures = [slack_error(429, "ratelimited", retry_after="8")]  # the replies fetch
    sleeps = Sleeps()
    thread = await SlackWorkSource(reader, MemoryStore(), sleep=sleeps).thread(CH, ROOT)
    assert sum(sleeps.waits) <= packet.RETRY_BUDGET_SECONDS
    assert sleeps.waits == [8.0, 8.0]  # the fetch, then one name; the rest fall back at once
    [message] = thread.messages
    assert message.author.text == "U0NEW"
    assert [m.display_name.text for m in message.mentions] == [f"U0P{i}" for i in range(5)]


async def test_related_spends_what_the_thread_left_not_a_new_budget() -> None:
    reader = FakeReader(
        {
            ROOT: [msg(ROOT, "progress bar")],
            "2.0": [msg("2.0", "progress bar", thread_ts="2.0")],
        }
    )
    sleeps = Sleeps()
    src = SlackWorkSource(reader, MemoryStore(), sleep=sleeps)
    reader.failures = [slack_error(429, "ratelimited", retry_after="10")] * 2
    thread = await src.thread(CH, ROOT)  # waits 10, 10: the whole budget
    reader.failures = [slack_error(429, "ratelimited", retry_after="1")]
    excerpts = await src.related(thread, index(summary("t", "Progress bar", ["2.0"])))
    assert excerpts == []  # no budget left, so no wait and no excerpt
    assert sleeps.waits == [10.0, 10.0]


async def test_an_exhausted_budget_returns_a_clean_error_and_records_nothing() -> None:
    from jotflow.models import RunKind as Kind

    store = MemoryStore()
    policy = parse_policy(
        f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"
"""
    )
    reader = FakeReader({ROOT: [msg(ROOT, "progress bar")]})
    reader.failures = [slack_error(429, "ratelimited", retry_after="10")] * 10
    run = await store.enqueue_run(Kind.DRAFTING, CH, ROOT)
    await store.claim_next_run(max_running=1)
    tools = checked_registry(read_tools(store, policy, source(reader, store)))
    grant = Grant(run.id, Kind.DRAFTING, CH, thread_ts=ROOT)
    with pytest.raises(ToolError, match="rate limiting"):
        await tools["get_work"].handler(grant, NoArguments())
    assert await store.packet_ref(run.id, CH, ROOT) is None


async def test_a_whole_get_work_under_endless_rate_limits_waits_at_most_the_budget() -> None:
    from jotflow.models import RunKind as Kind

    class AlwaysLimited(FakeReader):
        async def replies(
            self, channel: str, ts: str, *, cursor: str | None, limit: int
        ) -> Mapping[str, Any]:
            self.calls.append(("replies", ts))
            raise slack_error(429, "ratelimited", retry_after="10")

        async def user(self, user_id: str) -> Mapping[str, Any]:
            self.calls.append(("user", user_id))
            raise slack_error(429, "ratelimited", retry_after="10")

    store = MemoryStore()
    policy = parse_policy(
        f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"
"""
    )
    sleeps = Sleeps()
    work_source = SlackWorkSource(AlwaysLimited(), store, sleep=sleeps)
    run = await store.enqueue_run(Kind.DRAFTING, CH, ROOT)
    await store.claim_next_run(max_running=1)
    tools = checked_registry(read_tools(store, policy, work_source))
    grant = Grant(run.id, Kind.DRAFTING, CH, thread_ts=ROOT)
    with pytest.raises(ToolError, match="rate limiting"):
        await tools["get_work"].handler(grant, NoArguments())
    assert sum(sleeps.waits) <= packet.RETRY_BUDGET_SECONDS
    assert await store.packet_ref(run.id, CH, ROOT) is None
