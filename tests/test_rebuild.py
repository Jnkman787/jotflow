"""`jotflow rebuild` (#38): the store's index comes back from JotFlow's own Slack replies."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jotflow.commands import rebuild as rebuild_command
from jotflow.commands.rebuild import RefusedError, _oldest, rebuild
from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    UpdateProposal,
)
from jotflow.orchestrator.help import guide_text
from jotflow.orchestrator.ingest import BotIdentity
from jotflow.orchestrator.modes import MEANING
from jotflow.orchestrator.thread_notice import reply_text
from jotflow.policy import Policy
from jotflow.rebuild import plan_thread
from jotflow.store import SqliteStore, Store

CH = "C0IDEAS01"
OTHER = "C0BUGS001"
BOT = BotIdentity(user_id="U0JOTFLOW", bot_id="B0JOTFLOW")
SENTINEL = "SENTINEL-do-not-store-7f3a"
START = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}
API = {"repo": "acme-co/api", "nickname": "api", "description": "API"}
POLICY = Policy.model_validate({"channels": {CH: {"repos": [WEB, API]}, OTHER: {"repos": [WEB]}}})


def _reply(channel: str, url: str, *, update: bool = False) -> str:
    """The thread reply exactly as thread_notice writes it, so the two can't drift apart."""
    proposal: Any = (
        UpdateProposal(
            topic_id="t", repo="acme-co/web", issue_number=1, changes={"comment": "c"}, confidence=1
        )
        if update
        else IssueProposal(repo="acme-co/web", title="T", body="B", confidence=0.9)
    )
    record = ProposalRecord(
        id="p",
        channel_id=channel,
        thread_ts="1.0",
        run_id="r",
        state=ProposalState.APPLIED,
        proposal=proposal,
        content_hash="sha256:" + "0" * 64,
        approved_by="U0CAROL",
        created_at=START,
    ).model_copy(update={"result": ExecutionResult.model_construct(issue_url=url, error=None)})
    text = reply_text(POLICY, record)
    assert text is not None
    return text


def person(ts: str, text: str = f"an idea {SENTINEL}", **extra: Any) -> dict[str, Any]:
    return {"type": "message", "user": "U0ALICE", "ts": ts, "text": text, **extra}


def own(ts: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "user": BOT.user_id,
        "bot_id": BOT.bot_id,
        "ts": ts,
        "text": text,
        **extra,
    }


class FakeSlack:
    """Channel history and threads, paged two at a time so cursors are exercised."""

    def __init__(self, page: int = 2) -> None:
        self.history_of: dict[str, list[dict[str, Any]]] = {}
        self.threads: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.page = page
        self.calls: list[str] = []

    def thread(self, channel: str, parent: dict[str, Any], *replies: dict[str, Any]) -> None:
        parent = {**parent, "reply_count": len(replies), "thread_ts": parent["ts"]}
        self.history_of.setdefault(channel, []).append(parent)
        self.threads[(channel, parent["ts"])] = [
            parent,
            *({**r, "thread_ts": parent["ts"]} for r in replies),
        ]

    def post(self, channel: str, message: dict[str, Any]) -> None:
        self.history_of.setdefault(channel, []).append(message)

    def _page(self, items: list[dict[str, Any]], cursor: str | None) -> Mapping[str, Any]:
        start = int(cursor or 0)
        end = start + self.page
        more = end < len(items)
        return {
            "messages": items[start:end],
            "response_metadata": {"next_cursor": str(end) if more else ""},
        }

    async def history(
        self, channel: str, *, cursor: str | None, oldest: str | None, limit: int
    ) -> Mapping[str, Any]:
        self.calls.append(f"history {channel}")
        items = sorted(self.history_of.get(channel, []), key=lambda m: -float(m["ts"]))
        if oldest is not None:
            items = [m for m in items if float(m["ts"]) > float(oldest)]
        return self._page(items, cursor)

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        self.calls.append(f"replies {channel} {ts}")
        return self._page(self.threads.get((channel, ts), []), cursor)


WEB42 = "https://github.com/acme-co/web/issues/42"
API7 = "https://github.com/acme-co/api/issues/7"


def workspace() -> FakeSlack:
    """What JotFlow would have left in Slack, with a store to match it (see original())."""
    slack = FakeSlack()
    slack.post(CH, own("1000.000100", guide_text()))
    slack.post(
        CH,
        own(
            "1001.000100",
            f"Alice set JotFlow to propose mode in this channel.\n{MEANING[ChannelMode.PROPOSE]}",
        ),
    )
    slack.post(
        CH,
        own(
            "1002.000100",
            f"Alice {SENTINEL} set JotFlow to auto mode in this channel.\n"
            f"{MEANING[ChannelMode.AUTO]}",
        ),
    )
    # A thread that became web#42, with a follow-up that updated it.
    slack.thread(
        CH,
        person("1100.000100"),
        person("1100.000200"),
        own("1100.000300", f"Proposed issue {SENTINEL}"),
        own("1100.000400", _reply(CH, WEB42)),
        person("1100.000500", f"also {SENTINEL}"),
        person("1100.000600", "skip: lunch?"),
        own("1100.000700", _reply(CH, WEB42, update=True)),
    )
    # A thread that became api#7.
    slack.thread(CH, person("1200.000100"), own("1200.000200", _reply(CH, API7)))
    # A thread JotFlow drafted from but never wrote: nothing to restore.
    slack.thread(CH, person("1300.000100"), person("1300.000200"))
    # Someone pasting what looks like JotFlow's reply is not JotFlow.
    slack.thread(
        CH,
        person("1400.000100"),
        person("1400.000200", "Created <https://github.com/acme-co/web/issues/9|web#9>"),
    )
    # The same issue in another channel is its own topic there.
    slack.thread(OTHER, person("1500.000100"), own("1500.000200", _reply(OTHER, WEB42)))
    return slack


async def original(store: Store) -> None:
    """The store JotFlow had before the database was lost."""
    web = await store.create_topic(CH, "Progress bar on onboarding")
    await store.set_topic_issue(web.topic.id, "acme-co/web", 42)
    for ts, thread in [
        ("1100.000100", None),
        ("1100.000200", "1100.000100"),
        ("1100.000500", "1100.000100"),
    ]:
        await store.link_message(MessageRef(channel_id=CH, ts=ts, thread_ts=thread), web.topic.id)
    await store.mark_chatter(MessageRef(channel_id=CH, ts="1100.000600", thread_ts="1100.000100"))
    api = await store.create_topic(CH, "Rate limit headers")
    await store.set_topic_issue(api.topic.id, "acme-co/api", 7)
    await store.link_message(MessageRef(channel_id=CH, ts="1200.000100"), api.topic.id)
    other = await store.create_topic(OTHER, "Progress bar")
    await store.set_topic_issue(other.topic.id, "acme-co/web", 42)
    await store.link_message(MessageRef(channel_id=OTHER, ts="1500.000100"), other.topic.id)
    await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by="U0ALICE")
    assert await store.claim_notice(CH, "", "guide")


async def index(store: Store) -> dict[str, Any]:
    """The routing index rebuild promises: issues, their messages, chatter, mode."""
    out: dict[str, Any] = {}
    for channel in (CH, OTHER):
        issues: dict[tuple[str, int], list[tuple[str, str | None]]] = {}
        for record in await store.channel_topics(channel):
            topic = record.topic
            if topic.repo is None or topic.issue_number is None:
                continue
            issues[(topic.repo, topic.issue_number)] = sorted(
                (m.ts, m.thread_ts) for m in topic.messages
            )
        chatter = []
        for ts in ("1100.000600",):
            link = await store.get_message(channel, ts)
            if link is not None and link.chatter:
                chatter.append(ts)
        mode = await store.get_channel_mode(channel)
        out[channel] = {
            "issues": issues,
            "chatter": chatter,
            "mode": mode.mode if mode else None,
        }
    return out


async def run_rebuild(store: Store, slack: FakeSlack, **kwargs: Any) -> list[Any]:
    lines: list[str] = []
    options = {"oldest": None, "merge": False, "dry_run": False, **kwargs}
    return await rebuild(store, POLICY, slack, BOT, [CH, OTHER], say=lines.append, **options)


# --- box 1: delete the database, rebuild, and the index matches ---------------------------


async def test_deleting_the_database_and_rebuilding_restores_the_index(tmp_path: Path) -> None:
    path = tmp_path / "jotflow.db"
    store = await SqliteStore.open(str(path))
    await original(store)
    before = await index(store)
    await store.close()
    # Guard the comparison itself: the snapshot isn't trivially empty.
    assert before[CH]["chatter"] == ["1100.000600"]
    assert before[CH]["mode"] is ChannelMode.AUTO
    assert len(before[CH]["issues"]) == 2 and len(before[OTHER]["issues"]) == 1

    path.unlink()
    for leftover in tmp_path.glob("jotflow.db-*"):
        leftover.unlink()

    store = await SqliteStore.open(str(path))
    try:
        await run_rebuild(store, workspace())
        assert await index(store) == before
        # The guide was already posted, so JotFlow won't post and pin it again.
        assert not await store.claim_notice(CH, "", "guide")
        titles = {r.topic.title for r in await store.channel_topics(CH)}
        assert titles == {"web#42", "api#7"}
    finally:
        await store.close()


async def test_no_message_text_is_stored_or_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "jotflow.db"
    store = await SqliteStore.open(str(path))
    caplog.set_level(logging.DEBUG)
    try:
        await run_rebuild(store, workspace())
    finally:
        await store.close()
    for file in tmp_path.glob("jotflow.db*"):
        assert SENTINEL.encode() not in file.read_bytes()
    assert SENTINEL not in caplog.text


# --- what is and isn't JotFlow's ------------------------------------------------------------


def test_only_the_bots_own_reply_counts() -> None:
    plan = plan_thread(
        "1.0",
        [
            person("1.0"),
            person("2.0", "Created <https://github.com/acme-co/web/issues/9|web#9>"),
            {
                **person("3.0", "Created <https://github.com/acme-co/web/issues/8|web#8>"),
                "bot_id": "B0OTHER",
            },
        ],
        BOT,
    )
    assert plan.issues == [] and plan.messages == []


def test_messages_go_to_the_next_reply_and_follow_ups_to_the_last() -> None:
    plan = plan_thread(
        "1.0",
        [
            person("1.0"),
            own("2.0", _reply(CH, WEB42)),
            person("3.0"),
            own("4.0", _reply(CH, API7)),
            person("5.0"),
            {**person("6.0"), "subtype": "channel_join"},
        ],
        BOT,
    )
    assert plan.issues == [("acme-co/web", 42), ("acme-co/api", 7)]
    assert plan.messages == [("1.0", 0), ("3.0", 1), ("5.0", 1)]


# --- refusals and modes ---------------------------------------------------------------------


async def test_a_store_with_topics_is_refused_without_merge(tmp_path: Path) -> None:
    store = await SqliteStore.open(str(tmp_path / "jotflow.db"))
    try:
        await store.create_topic(CH, "Already here")
        with pytest.raises(RefusedError, match="--merge"):
            await run_rebuild(store, workspace())
    finally:
        await store.close()


async def test_merge_adds_only_what_is_missing_and_overwrites_nothing(tmp_path: Path) -> None:
    store = await SqliteStore.open(str(tmp_path / "jotflow.db"))
    try:
        mine = await store.create_topic(CH, "A person's own topic")
        ref = MessageRef(channel_id=CH, ts="1100.000200", thread_ts="1100.000100")
        await store.link_message(ref, mine.topic.id, by="person")
        await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0BOB")
        await run_rebuild(store, workspace(), merge=True)
        kept = await store.get_message(CH, "1100.000200")
        assert kept is not None and kept.topic_id == mine.topic.id
        mode = await store.get_channel_mode(CH)
        assert mode is not None and mode.mode is ChannelMode.PAUSED
        assert (await index(store))[CH]["issues"][("acme-co/api", 7)] == [("1200.000100", None)]
        # Running it again restores nothing twice.
        again = await run_rebuild(store, workspace(), merge=True)
        assert all(r.topics == 0 and r.linked == 0 for _, r in again)
    finally:
        await store.close()


async def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = await SqliteStore.open(str(tmp_path / "jotflow.db"))
    try:
        results = await run_rebuild(store, workspace(), dry_run=True)
        assert [r for _, r in results] == [None, None]
        assert results[0][0].issue_count == 2
        assert await store.channel_topics(CH) == []
        assert await store.get_channel_mode(CH) is None
    finally:
        await store.close()


async def test_since_reads_only_newer_threads(tmp_path: Path) -> None:
    store = await SqliteStore.open(str(tmp_path / "jotflow.db"))
    try:
        await run_rebuild(store, workspace(), oldest="1150.0")
        issues = (await index(store))[CH]["issues"]
        assert set(issues) == {("acme-co/api", 7)}
    finally:
        await store.close()


def test_since_takes_days_or_all() -> None:
    assert _oldest("all", 1000.0) is None
    assert _oldest("1d", 100000.0) == f"{100000.0 - 86400:.6f}"
    with pytest.raises(ValueError, match="90d"):
        _oldest("ninety", 1.0)


def test_rebuild_refuses_while_jotflow_runs_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    beat = tmp_path / "heartbeat"
    beat.write_text(str(time.time()))
    monkeypatch.setenv("JOTFLOW_HEARTBEAT_FILE", str(beat))
    args = argparse.Namespace(since="all", channel=None, merge=False, dry_run=False, no_lock=False)
    assert rebuild_command.run(args) == rebuild_command.EXIT_REFUSED
    assert "docker compose stop jotflow" in capsys.readouterr().err


def test_the_summary_line_has_counts_only() -> None:
    from jotflow.commands.rebuild import _summary
    from jotflow.rebuild import ChannelPlan, Restored, ThreadPlan

    plan = ChannelPlan(CH, [ThreadPlan("1.0", [("acme-co/web", 42)])], ChannelMode.AUTO, True, 9)
    line = _summary(plan, Restored(topics=1, linked=3, chatter=1, mode=True, guide=True))
    assert line == (
        "C0IDEAS01: 9 messages read, 1 threads with issues, 1 issues; 1 topics restored, "
        "3 messages linked, 1 skip: messages marked, mode auto, guide noted"
    )
    assert _summary(plan, None).endswith("(dry run, nothing written)")


class _Response:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def conversations_history(self, **kwargs: Any) -> _Response:
        self.calls.append({"history": kwargs})
        return _Response({"messages": []})

    async def conversations_replies(self, **kwargs: Any) -> _Response:
        self.calls.append({"replies": kwargs})
        return _Response({"messages": []})


async def test_the_slack_adapter_passes_oldest_only_when_given() -> None:
    from jotflow.commands.rebuild import BoltRebuildSlack

    client = _Client()
    slack = BoltRebuildSlack(client)  # type: ignore[arg-type]
    await slack.history(CH, cursor=None, oldest=None, limit=200)
    await slack.history(CH, cursor="c", oldest="5.0", limit=200)
    await slack.replies(CH, "1.0", cursor=None, limit=200)
    assert client.calls == [
        {"history": {"channel": CH, "cursor": None, "limit": 200}},
        {"history": {"channel": CH, "cursor": "c", "limit": 200, "oldest": "5.0"}},
        {"replies": {"channel": CH, "ts": "1.0", "cursor": None, "limit": 200}},
    ]


async def test_a_windowed_rebuild_that_finds_no_mode_says_to_check_it(tmp_path: Path) -> None:
    store = await SqliteStore.open(str(tmp_path / "jotflow.db"))
    lines: list[str] = []
    try:
        await rebuild(
            store,
            POLICY,
            workspace(),
            BOT,
            [CH],
            oldest="1150.0",
            merge=False,
            dry_run=False,
            say=lines.append,
        )
    finally:
        await store.close()
    assert "check C0IDEAS01's mode with /jotflow mode" in lines[-1]


# --- security review (#38): validation, permalinks, pacing, one writer -------------------------


def test_a_reply_for_a_repo_not_in_the_channel_is_skipped_and_counted() -> None:
    plan = plan_thread(
        "1.0",
        [person("1.0"), own("2.0", _reply(CH, "https://github.com/acme-co/old/issues/3"))],
        BOT,
        repos=["acme-co/web"],
    )
    assert plan.issues == [] and plan.unknown_repos == 1


async def test_permalinks_are_stored_as_ingest_stores_them(tmp_path: Path) -> None:
    store = await SqliteStore.open(str(tmp_path / "jotflow.db"))
    try:
        await run_rebuild(store, workspace())
        link = await store.get_message(CH, "1100.000200")
        assert link is not None and link.ref.permalink is None
        assert link.ref.thread_ts == "1100.000100"
    finally:
        await store.close()


class _RateLimited(FakeSlack):
    def __init__(self) -> None:
        super().__init__()
        self.limited = 1

    async def history(
        self, channel: str, *, cursor: str | None, oldest: str | None, limit: int
    ) -> Mapping[str, Any]:
        if self.limited:
            self.limited -= 1
            from slack_sdk.errors import SlackApiError
            from slack_sdk.web.async_slack_response import AsyncSlackResponse

            response = AsyncSlackResponse(
                client=None,  # type: ignore[arg-type]
                http_verb="POST",
                api_url="https://slack.com/api/conversations.history",
                req_args={},
                data={"ok": False, "error": "ratelimited"},
                headers={"Retry-After": "3"},
                status_code=429,
            )
            raise SlackApiError("ratelimited", response)
        return await super().history(channel, cursor=cursor, oldest=oldest, limit=limit)


async def test_a_rate_limited_page_is_retried_with_the_shared_backoff() -> None:
    from jotflow.rebuild import collect

    slack = _RateLimited()
    slack.thread(CH, person("1.0"), own("2.0", _reply(CH, WEB42)))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    plan = await collect(slack, CH, BOT, repos=["acme-co/web"], sleep=sleep)
    assert waits == [3.0]
    assert plan.issue_count == 1


async def test_only_one_process_holds_the_store(tmp_path: Path) -> None:
    from jotflow.orchestrator.service import open_sqlite
    from jotflow.store.lock import StoreBusyError

    path = str(tmp_path / "jotflow.db")
    running = await open_sqlite(path)  # what jotflow run opens
    try:
        with pytest.raises(StoreBusyError, match="stop it first"):
            await SqliteStore.open(path, exclusive=True)
    finally:
        await running.close()
    # Closing releases it, so a rebuild can now take it.
    rebuilding = await SqliteStore.open(path, exclusive=True)
    await rebuilding.close()


# --- the lock on filesystems that refuse flock, symlinks, and crashes -------------------------


def _no_flock(monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from jotflow.store import lock

    def refuse(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock.fcntl, "flock", refuse)


async def test_jotflow_run_warns_and_carries_on_where_flock_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import os

    from jotflow.orchestrator.service import open_sqlite
    from jotflow.store.lock import UNSUPPORTED

    _no_flock(monkeypatch)
    before = len(os.listdir("/proc/self/fd"))
    store = await open_sqlite(str(tmp_path / "jotflow.db"))
    try:
        assert UNSUPPORTED in caplog.text
        assert await store.channel_topics(CH) == []
    finally:
        await store.close()
    assert len(os.listdir("/proc/self/fd")) == before  # the lock's descriptor was closed


async def test_rebuild_refuses_where_flock_is_refused_unless_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow.store.lock import LockUnsupportedError

    _no_flock(monkeypatch)
    path = str(tmp_path / "jotflow.db")
    with pytest.raises(LockUnsupportedError):
        await SqliteStore.open(path, exclusive=True)
    store = await SqliteStore.open(path, exclusive=False)  # what --no-lock opens
    await store.close()


def test_the_no_lock_flag_says_it_skips_the_safety_check() -> None:
    parser = argparse.ArgumentParser()
    rebuild_command.add_arguments(parser)
    assert "Skip the safety check" in parser.format_help()
    assert parser.parse_args(["--no-lock"]).no_lock is True


def test_a_symlinked_lock_file_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    from jotflow.store import StoreError
    from jotflow.store.lock import acquire, lock_path

    target = tmp_path / "elsewhere"
    lock_path(tmp_path / "jotflow.db").symlink_to(target)
    with pytest.raises(StoreError, match="the lock file is a symlink"):
        acquire(tmp_path / "jotflow.db")
    assert not target.exists()


def test_a_lock_held_by_a_killed_process_is_free_again(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess
    import sys

    from jotflow.store.lock import acquire

    db = tmp_path / "jotflow.db"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from jotflow.store.lock import acquire\n"
            "held = acquire(sys.argv[1])\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
            str(db),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        from jotflow.store.lock import StoreBusyError

        with pytest.raises(StoreBusyError):
            acquire(db)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        held = acquire(db)
        assert held is not None
        held.release()
    finally:
        if child.poll() is None:
            child.kill()
