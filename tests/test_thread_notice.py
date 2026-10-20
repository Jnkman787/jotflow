"""The issue link in the thread and the reaction on its source messages (#32)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import (
    ExecutionResult,
    IssueChanges,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    UpdateProposal,
)
from jotflow.orchestrator.thread_notice import (
    FIRST_RETRY,
    BoltThreadSlack,
    ThreadNotice,
    reply_text,
)
from jotflow.policy import Policy
from jotflow.store import MemoryStore, SqliteStore, Store
from jotflow.store.hashing import content_hash

CH = "C0IDEAS01"
THREAD = "1760781600.000100"
REPLY = "1760781660.000100"
START = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
URL = "https://github.com/acme-co/web/issues/7"
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}


def policy(**top: Any) -> Policy:
    return Policy.model_validate({**top, "channels": {CH: {"repos": [WEB]}}})


class SlackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class FakeThreadSlack:
    def __init__(self) -> None:
        self.reactions: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str, str]] = []
        self.react_errors: dict[str, str] = {}
        self.reply_failures: list[Exception] = []

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        if ts in self.react_errors:
            raise SlackError(self.react_errors[ts])
        self.reactions.append((channel_id, ts, name))

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        if self.reply_failures:
            raise self.reply_failures.pop(0)
        self.replies.append((channel_id, thread_ts, text))


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


SOURCES = [
    MessageRef(channel_id=CH, ts=THREAD),
    MessageRef(channel_id=CH, ts=REPLY, thread_ts=THREAD),
    MessageRef(channel_id=CH, ts=THREAD),  # listed twice: one reaction
]


async def applied(
    store: Store,
    proposal_id: str = "p1",
    *,
    url: str = URL,
    update: bool = False,
) -> None:
    topic_id = (await store.create_topic(CH, "Export")).topic.id if update else None
    draft: IssueProposal | UpdateProposal = (
        UpdateProposal(
            topic_id=topic_id or "",
            repo="acme-co/web",
            issue_number=42,
            changes=IssueChanges(comment="Keep the order."),
            sources=SOURCES,
            confidence=0.9,
        )
        if update
        else IssueProposal(
            repo="acme-co/web", title="Dark mode", body="Add it.", sources=SOURCES, confidence=0.9
        )
    )
    record = ProposalRecord(
        id=proposal_id,
        channel_id=CH,
        thread_ts=THREAD,
        run_id="run-1",
        proposal=draft,
        created_at=START,
    )
    await store.add_proposal(record)
    await store.transition(
        proposal_id,
        ProposalState.APPROVED,
        approved_by="U0CAROL",
        expected_hash=content_hash(draft),
    )
    await store.transition(proposal_id, ProposalState.EXECUTING)
    await store.transition(
        proposal_id, ProposalState.APPLIED, result=ExecutionResult(issue_url=url)
    )


def notice(store: Store, slack: FakeThreadSlack, clock: Clock | None = None, **top: Any) -> Any:
    return ThreadNotice(store, policy(**top), slack, clock=clock or Clock())


# --- the reply and the reactions ---------------------------------------------------


async def test_an_applied_issue_gets_one_reply_and_its_reactions(store: Store) -> None:
    slack = FakeThreadSlack()
    threads = notice(store, slack)
    await applied(store)
    assert await threads.tell() == 1
    assert await threads.tell() == 0
    assert slack.replies == [(CH, THREAD, f"Created <{URL}|web#7>")]
    assert slack.reactions == [(CH, THREAD, "ticket"), (CH, REPLY, "ticket")]
    assert await store.threads_due() == []


async def test_an_update_says_updated(store: Store) -> None:
    slack = FakeThreadSlack()
    await applied(store, url="https://github.com/acme-co/web/issues/42", update=True)
    await notice(store, slack).tell()
    assert slack.replies[0][2] == "Updated <https://github.com/acme-co/web/issues/42|web#42>"


async def test_the_reaction_can_be_turned_off(store: Store) -> None:
    slack = FakeThreadSlack()
    await applied(store)
    await notice(store, slack, issue_reaction="").tell()
    assert slack.reactions == [] and len(slack.replies) == 1


async def test_the_reply_carries_the_url_even_with_issue_links_none(store: Store) -> None:
    slack = FakeThreadSlack()
    await applied(store)
    await notice(store, slack, issue_links="none").tell()
    assert URL in slack.replies[0][2]


async def test_a_missing_or_reacted_message_does_not_stop_the_reply(store: Store) -> None:
    slack = FakeThreadSlack()
    slack.react_errors = {THREAD: "already_reacted", REPLY: "message_not_found"}
    await applied(store)
    assert await notice(store, slack).tell() == 1
    assert len(slack.replies) == 1


# --- retries never double the reply -------------------------------------------------------


async def test_a_failed_reply_is_retried_and_posted_once(store: Store) -> None:
    slack, clock = FakeThreadSlack(), Clock()
    threads = notice(store, slack, clock)
    slack.reply_failures = [SlackError("ratelimited")]
    await applied(store)
    assert await threads.tell() == 0
    assert await threads.tell() == 0  # still waiting
    clock.now += FIRST_RETRY
    assert await threads.tell() == 1
    assert len(slack.replies) == 1
    assert len(slack.reactions) == 4  # reacted twice: harmless, and only the reply counts


async def test_a_failed_reaction_is_retried_before_any_reply(store: Store) -> None:
    slack, clock = FakeThreadSlack(), Clock()
    threads = notice(store, slack, clock)
    slack.react_errors = {REPLY: "ratelimited"}
    await applied(store)
    assert await threads.tell() == 0
    assert slack.replies == []
    slack.react_errors = {}
    clock.now += FIRST_RETRY
    assert await threads.tell() == 1
    assert len(slack.replies) == 1


@pytest.mark.parametrize("code", ["channel_not_found", "is_archived", "thread_not_found"])
async def test_a_gone_thread_is_given_up(
    store: Store, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    slack = FakeThreadSlack()
    slack.reply_failures = [SlackError(code)]
    await applied(store)
    assert await notice(store, slack).tell() == 0
    assert await store.threads_due() == []
    assert code in caplog.text


async def test_the_log_never_holds_the_errors_text(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    slack = FakeThreadSlack()
    slack.reply_failures = [RuntimeError("SECRET thread text")]
    await applied(store)
    await notice(store, slack).tell()
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text


async def test_a_paused_channel_waits(store: Store) -> None:
    from jotflow.models import ChannelMode

    slack = FakeThreadSlack()
    await applied(store)
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
    assert await notice(store, slack).tell() == 0
    await store.set_channel_mode(CH, ChannelMode.PROPOSE, changed_by="U0LEAD")
    assert await notice(store, slack).tell() == 1


# --- what is said ---------------------------------------------------------------------------


def _record(url: str | None, channel: str = CH) -> ProposalRecord:
    record = ProposalRecord(
        id="p1",
        channel_id=channel,
        thread_ts=THREAD,
        run_id="run-1",
        state=ProposalState.APPLIED,
        proposal=IssueProposal(repo="acme-co/web", title="T", body="B", confidence=0.9),
        content_hash="sha256:" + "0" * 64,
        approved_by="U0CAROL",
        created_at=START,
    )
    # Attached without validation: a check below the model's own (defence in depth).
    result = ExecutionResult.model_construct(issue_url=url, error=None) if url else None
    return record.model_copy(update={"result": result})


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme-co/web/issues/7|click>",
        "https://evil.example/acme-co/web/issues/7",
        "https://github.com/acme-co/web/pull/7",
    ],
)
def test_only_a_github_issue_url_is_ever_linked(url: str) -> None:
    assert reply_text(policy(), _record(url)) is None


def test_a_repo_the_channel_no_longer_lists_is_named_in_full() -> None:
    other = "https://github.com/acme-co/api/issues/3"
    assert reply_text(policy(), _record(other)) == f"Created <{other}|acme-co/api#3>"


async def test_an_applied_proposal_without_a_url_is_marked_and_skipped(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow.orchestrator import thread_notice

    slack = FakeThreadSlack()
    await applied(store)
    monkeypatch.setattr(thread_notice, "reply_text", lambda policy, record, repos=None: None)
    assert await notice(store, slack).tell() == 0
    assert slack.replies == [] and await store.threads_due() == []


# --- the Bolt adapter -------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def reactions_add(self, **kwargs: Any) -> None:
        self.calls.append(("reactions_add", kwargs))

    async def chat_postMessage(self, **kwargs: Any) -> None:
        self.calls.append(("chat_postMessage", kwargs))


async def test_the_adapter_reacts_and_replies_without_unfurling() -> None:
    client = FakeClient()
    slack = BoltThreadSlack(client)
    await slack.react(CH, THREAD, "ticket")
    await slack.reply(CH, THREAD, "Created")
    assert client.calls == [
        ("reactions_add", {"channel": CH, "timestamp": THREAD, "name": "ticket"}),
        (
            "chat_postMessage",
            {
                "channel": CH,
                "thread_ts": THREAD,
                "text": "Created",
                "unfurl_links": False,
                "unfurl_media": False,
            },
        ),
    ]


@pytest.mark.parametrize("code", ["invalid_name", "not_in_channel", "is_archived"])
async def test_a_reaction_slack_will_never_take_does_not_hold_back_the_reply(
    store: Store, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    slack = FakeThreadSlack()
    slack.react_errors = {THREAD: code, REPLY: code}
    await applied(store)
    assert await notice(store, slack).tell() == 1
    assert len(slack.replies) == 1
    assert code in caplog.text
