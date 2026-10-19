"""The write tools (#15): scoping, policy, caps, idempotency, and the result rules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from slack_sdk.errors import SlackApiError

from jotflow.models import MessageRef, ProposalState, Run, RunKind, RunStatus
from jotflow.orchestrator.slack import SlackThreadPoster
from jotflow.policy import parse_policy
from jotflow.store import MemoryStore
from jotflow.whiteboard import write_tools as wt
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError, checked_registry
from jotflow.whiteboard.write_tools import (
    AskChannelArgs,
    LinkMessageArgs,
    PostError,
    ProposeIssueArgs,
    ProposeUpdateArgs,
    RecordResultArgs,
    WriteTools,
    args_hash,
    clean_question,
    write_tools,
)

CH = "C0EXAMPLE1"
OTHER = "C0EXAMPLE2"
ROOT = "1792310400.000100"
REPLY = "1792310460.000100"
POLICY = parse_policy(
    f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app"
labels = ["bug", "enhancement"]

[[channels.{CH}.repos]]
repo = "acme-co/api"
nickname = "api"
description = "The API"

[channels.{OTHER}]

[[channels.{OTHER}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app"
"""
)
DRAFT = Grant(run_id="run-1", kind=RunKind.DRAFTING, channel_id=CH, thread_ts=ROOT)
DRAFT_2 = Grant(run_id="run-2", kind=RunKind.DRAFTING, channel_id=CH, thread_ts=ROOT)
ELSEWHERE = Grant(run_id="run-9", kind=RunKind.DRAFTING, channel_id=OTHER, thread_ts=ROOT)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Poster:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.error: Exception | None = None
        self.delay = 0.0

    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        self.posts.append((channel_id, thread_ts, text))
        return f"1792310999.{len(self.posts):06d}"


class Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"prop-{self.n}"


@pytest.fixture
def clock() -> Clock:
    return Clock()


class LiveStore(MemoryStore):
    """A store where each run is running, built from the grant registered for it.

    Unregistered runs are drafting runs in CH on ROOT, except ELSEWHERE's run-9.
    """

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock=clock)
        self.ended: set[str] = set()
        self.grants: dict[str, Grant] = {}

    def register(self, grant: Grant) -> Grant:
        self.grants[grant.run_id] = grant
        return grant

    async def get_run(self, run_id: str) -> Run | None:
        real = await super().get_run(run_id)
        if real is not None:
            return real
        grant = self.grants.get(run_id) or Grant(
            run_id, RunKind.DRAFTING, OTHER if run_id == "run-9" else CH, thread_ts=ROOT
        )
        ended = run_id in self.ended
        return Run(
            id=run_id,
            kind=grant.kind,
            status=RunStatus.SUCCEEDED if ended else RunStatus.RUNNING,
            channel_id=grant.channel_id,
            thread_ts=grant.thread_ts or ROOT,
            proposal_id=grant.proposal_id,
            created_at=datetime(2026, 10, 18, 11, 0, tzinfo=UTC),
            deadline=datetime(2026, 10, 18, 11, 30, tzinfo=UTC),
            started_at=datetime(2026, 10, 18, 11, 0, tzinfo=UTC),
            finished_at=datetime(2026, 10, 18, 11, 5, tzinfo=UTC) if ended else None,
        )


@pytest.fixture
def store(clock: Clock) -> LiveStore:
    return LiveStore(clock)


@pytest.fixture
def poster() -> Poster:
    return Poster()


@pytest.fixture
async def tools(store: MemoryStore, poster: Poster, clock: Clock) -> WriteTools:
    packet = [
        MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT),
        MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT),
    ]
    await store.record_packet("run-1", packet)
    await store.record_packet("run-2", packet)
    return WriteTools(store, POLICY, poster, clock=clock, new_id=Ids())


def link(ts: str = ROOT, **fields: Any) -> LinkMessageArgs:
    base: dict[str, Any] = {"ts": ts, "decision": "new_topic", "title": "Export as CSV"}
    base.update(fields)
    return LinkMessageArgs.model_validate({**base, "confidence": base.get("confidence", 0.9)})


async def new_topic(tools: WriteTools, grant: Grant = DRAFT) -> str:
    result: Any = await tools.link_message(grant, link())
    return str(result.topic_id)


def issue(topic_id: str, **fields: Any) -> ProposeIssueArgs:
    base: dict[str, Any] = {
        "topic_id": topic_id,
        "repo": "Acme-Co/Web",
        "title": "Export the dashboard table as CSV",
        "body": "People want a CSV export.",
        "labels": ["Enhancement"],
        "sources": [{"channel_id": OTHER, "ts": ROOT}],
        "confidence": 0.8,
    }
    base.update(fields)
    return ProposeIssueArgs.model_validate(base)


# link_message


async def test_new_topic_links_the_message_with_its_packet_thread(
    tools: WriteTools, store: MemoryStore
) -> None:
    result: Any = await tools.link_message(DRAFT, link(REPLY))
    stored = await store.get_message(CH, REPLY)
    assert stored is not None
    assert stored.ref.thread_ts == ROOT
    assert stored.topic_id == result.topic_id
    assert result.decided_by == "agent"
    topic = await store.get_topic(result.topic_id)
    assert topic is not None and topic.topic.title == "Export as CSV"


async def test_existing_topic_and_chatter(tools: WriteTools, store: MemoryStore) -> None:
    topic_id = await new_topic(tools)
    joined: Any = await tools.link_message(
        DRAFT, link(REPLY, decision="existing_topic", topic_id=topic_id, title=None)
    )
    assert joined.topic_id == topic_id
    chatter: Any = await tools.link_message(DRAFT_2, link(REPLY, decision="chatter", title=None))
    assert chatter.chatter is True


@pytest.mark.parametrize(
    ("grant", "ts"),
    [(DRAFT, "9.000100"), (ELSEWHERE, ROOT)],
)
async def test_only_messages_in_the_runs_own_packet(
    tools: WriteTools, grant: Grant, ts: str
) -> None:
    with pytest.raises(ToolError, match="is not in this run's packet"):
        await tools.link_message(grant, link(ts))


async def test_a_persons_decision_stands(tools: WriteTools, store: MemoryStore) -> None:
    await store.mark_chatter(MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT), by="person")
    result: Any = await tools.link_message(DRAFT, link(REPLY))
    assert (result.chatter, result.decided_by, result.topic_id) == (True, "person", None)
    assert await store.channel_topics(CH) == []  # no orphan topic was made


async def test_a_topic_in_another_channel_reads_as_missing(
    tools: WriteTools, store: MemoryStore
) -> None:
    other = await store.create_topic(OTHER, "Elsewhere")
    with pytest.raises(ToolError, match=f"no topic {other.topic.id}"):
        await tools.link_message(
            DRAFT, link(decision="existing_topic", topic_id=other.topic.id, title=None)
        )


async def test_a_title_that_cleans_to_nothing(tools: WriteTools) -> None:
    with pytest.raises(ToolError, match="no words left"):
        await tools.link_message(DRAFT, link(title="<!channel> https://evil.example/x"))


@pytest.mark.parametrize(
    "fields",
    [
        {"decision": "new_topic", "title": None},
        {"decision": "new_topic", "topic_id": "t"},
        {"decision": "existing_topic", "title": None},
        {"decision": "chatter"},
    ],
)
def test_link_arguments_must_fit_the_decision(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        link(**fields)


async def test_links_are_capped(tools: WriteTools, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wt, "MAX_LINKS_PER_RUN", 1)
    await tools.link_message(DRAFT, link())
    with pytest.raises(ToolError, match="used its 1 message links"):
        await tools.link_message(DRAFT, link(REPLY, decision="chatter", title=None))


# Idempotency


async def test_an_identical_repeat_returns_the_first_result(
    tools: WriteTools, store: MemoryStore
) -> None:
    first = await tools.link_message(DRAFT, link())
    again = await tools.link_message(DRAFT, link())
    assert again == first
    assert len(await store.channel_topics(CH)) == 1
    assert await store.count_tool_calls("run-1", ["link_message"]) == 1


async def test_the_same_call_from_another_run_is_a_new_call(
    tools: WriteTools, store: MemoryStore
) -> None:
    await tools.link_message(DRAFT, link())
    await tools.link_message(DRAFT_2, link())
    assert len(await store.channel_topics(CH)) == 2


def test_args_hash_ignores_key_order() -> None:
    a = AskChannelArgs.model_validate({"text": "x"})
    assert args_hash("ask_channel", a) == args_hash("ask_channel", AskChannelArgs(text="x"))
    assert args_hash("ask_channel", a) != args_hash("link_message", a)


async def test_a_timeout_mid_write_cannot_cut_it_or_duplicate_it(
    tools: WriteTools, poster: Poster, store: MemoryStore
) -> None:
    poster.delay = 0.1
    with anyio.move_on_after(0.02):
        await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    # The shielded post and its ledger entry both finished despite the timeout.
    assert len(poster.posts) == 1
    assert await store.count_tool_calls("run-1", ["ask_channel"]) == 1
    poster.delay = 0
    retried: Any = await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    assert retried.posted_ts == "1792310999.000001"
    assert len(poster.posts) == 1


async def test_concurrent_identical_calls_write_once(tools: WriteTools, store: MemoryStore) -> None:
    results = await asyncio.gather(*(tools.link_message(DRAFT, link()) for _ in range(5)))
    assert len({r.model_dump_json() for r in results}) == 1
    assert len(await store.channel_topics(CH)) == 1


# propose_issue


async def test_propose_issue_uses_the_grant_policy_and_packet(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(DRAFT, issue(topic_id, participants=["U0BOB", "U0BOB"]))
    record = await store.get_proposal(result.proposal_id)
    assert record is not None
    assert (record.channel_id, record.thread_ts, record.run_id) == (CH, ROOT, "run-1")
    assert record.state is ProposalState.PENDING
    assert record.participants == ["U0BOB"]
    draft: Any = record.proposal
    assert draft.repo == "acme-co/web"  # the policy's own spelling
    assert draft.labels == ["enhancement"]  # the list's own spelling
    # The source came from the packet, so its channel is the grant's.
    assert draft.sources == [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)]


async def test_a_newer_proposal_supersedes_the_thread_s_pending_one(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    first: Any = await tools.propose_issue(DRAFT, issue(topic_id))
    second: Any = await tools.propose_issue(DRAFT, issue(topic_id, title="Export as CSV, v2"))
    assert second.superseded == [first.proposal_id]


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"topic_id": None}, "name the topic"),
        ({"topic_id": "topic-missing"}, "no topic topic-missing"),
        ({"repo": "acme-co/secret"}, "is not one of channel"),
        ({"labels": ["wontfix"]}, "label list"),
        ({"sources": [{"channel_id": CH, "ts": "9.000100"}]}, "not in this run's packet"),
        ({"assignees": ["acme-bob"]}, "is not the linked GitHub login of a participant"),
    ],
)
async def test_propose_issue_refusals(
    tools: WriteTools, fields: dict[str, Any], problem: str
) -> None:
    topic_id = await new_topic(tools)
    merged = {"topic_id": topic_id, **fields}
    args = issue(merged.pop("topic_id"), **merged)
    with pytest.raises(ToolError, match=problem):
        await tools.propose_issue(DRAFT, args)


async def test_an_assignee_must_be_a_linked_participant(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    await store.link_identity("U0BOB", "acme-bob")
    result: Any = await tools.propose_issue(
        DRAFT, issue(topic_id, participants=["U0BOB"], assignees=["Acme-Bob"])
    )
    assert result.state is ProposalState.PENDING


def test_participants_are_slack_user_ids() -> None:
    with pytest.raises(ValueError, match="Slack user ids"):
        issue("t", participants=["bob"])
    with pytest.raises(ValueError, match="at most 50"):
        issue("t", participants=[f"U0X{i:03d}" for i in range(51)])


async def test_a_topic_with_an_issue_needs_propose_update(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    with pytest.raises(ToolError, match="already has issue acme-co/web#42; use propose_update"):
        await tools.propose_issue(DRAFT, issue(topic_id))


async def test_proposals_are_capped(tools: WriteTools, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wt, "MAX_PROPOSALS_PER_RUN", 1)
    topic_id = await new_topic(tools)
    await tools.propose_issue(DRAFT, issue(topic_id))
    with pytest.raises(ToolError, match="used its 1 proposals"):
        await tools.propose_issue(DRAFT, issue(topic_id, title="Another"))


# propose_update


def update(topic_id: str, **fields: Any) -> ProposeUpdateArgs:
    base: dict[str, Any] = {
        "topic_id": topic_id,
        "repo": "acme-co/web",
        "issue_number": 42,
        "changes": {"comment": "Also apply the date filter.", "add_labels": ["BUG"]},
        "sources": [{"channel_id": CH, "ts": REPLY}],
        "confidence": 0.9,
    }
    base.update(fields)
    return ProposeUpdateArgs.model_validate(base)


async def test_propose_update_targets_only_the_topics_own_issue(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    result: Any = await tools.propose_update(DRAFT, update(topic_id))
    record = await store.get_proposal(result.proposal_id)
    assert record is not None
    changes: Any = record.proposal
    assert changes.changes.add_labels == ["bug"]
    for wrong in ({"issue_number": 43}, {"repo": "acme-co/api"}):
        with pytest.raises(ToolError, match="is not linked to"):
            await tools.propose_update(DRAFT, update(topic_id, **wrong))


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"comment": "x", "remove_labels": ["b\u200bug"]}, "a label to remove must not contain"),
        ({"comment": "x", "remove_labels": ["bug\u202e"]}, "a label to remove must not contain"),
        ({"comment": "x", "remove_labels": [" bug"]}, "a label to remove must not start"),
        ({"comment": "x", "remove_assignees": ["not a login"]}, "not a GitHub login"),
    ],
)
async def test_removals_are_checked_without_an_allowlist(
    tools: WriteTools, store: MemoryStore, changes: dict[str, Any], problem: str
) -> None:
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    with pytest.raises(ToolError, match=problem):
        await tools.propose_update(DRAFT, update(topic_id, changes=changes))


async def test_removing_a_label_outside_the_list_is_fine(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    changes = {"comment": "x", "remove_labels": ["wontfix"], "remove_assignees": ["acme-bob"]}
    result: Any = await tools.propose_update(DRAFT, update(topic_id, changes=changes))
    assert result.state is ProposalState.PENDING


async def test_propose_update_on_a_topic_without_an_issue(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="is not linked to acme-co/web#42"):
        await tools.propose_update(DRAFT, update(topic_id))


async def test_propose_update_assignees_follow_the_same_rule(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    args = update(topic_id, changes={"comment": "x", "add_assignees": ["acme-bob"]})
    with pytest.raises(ToolError, match="not the linked GitHub login"):
        await tools.propose_update(DRAFT, args)


# ask_channel


async def test_a_question_goes_only_to_the_grants_thread_cleaned(
    tools: WriteTools, poster: Poster
) -> None:
    result: Any = await tools.ask_channel(
        DRAFT, AskChannelArgs(text="<!channel> @here Which repo, <https://evil.example|web>?")
    )
    [(channel, thread, text)] = poster.posts
    assert (channel, thread) == (CH, ROOT)
    assert "<!" not in text and "@here" not in text
    assert text == "Which repo, &lt;https://evil.example|web&gt;?"
    assert result.posted_ts == "1792310999.000001"


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("Is this a bug?", "Is this a bug?"),
        ("<!subteam^S123|@devs> ping", "ping"),
        ("@everyone a & b", "a &amp; b"),
        ("email bob@channel.example", "email bob@channel.example"),
        ("tab\tok\nline\x07bell", "tab\tok\nlinebell"),
    ],
)
def test_clean_question(text: str, cleaned: str) -> None:
    assert clean_question(text) == cleaned


async def test_an_empty_question_after_cleaning_is_refused(tools: WriteTools) -> None:
    with pytest.raises(ToolError, match="empty once broadcast mentions are removed"):
        await tools.ask_channel(DRAFT, AskChannelArgs(text="<!here> @channel"))


async def test_questions_are_rate_limited_per_thread_across_runs(
    tools: WriteTools, clock: Clock
) -> None:
    await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    with pytest.raises(ToolError, match="in the last 30 seconds"):
        await tools.ask_channel(DRAFT_2, AskChannelArgs(text="Who owns this?"))
    clock.tick(31)
    await tools.ask_channel(DRAFT_2, AskChannelArgs(text="Who owns this?"))


async def test_questions_are_capped_per_run(tools: WriteTools, clock: Clock) -> None:
    for text in ("One?", "Two?"):
        await tools.ask_channel(DRAFT, AskChannelArgs(text=text))
        clock.tick(31)
    with pytest.raises(ToolError, match="used its 2 questions"):
        await tools.ask_channel(DRAFT, AskChannelArgs(text="Three?"))


async def test_a_failed_post_records_nothing_so_it_can_be_retried(
    tools: WriteTools, poster: Poster, store: MemoryStore
) -> None:
    poster.error = PostError("channel_not_found")
    with pytest.raises(ToolError, match="Slack did not post the question"):
        await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    assert await store.count_tool_calls("run-1", ["ask_channel"]) == 0
    poster.error = None
    await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    assert len(poster.posts) == 1


# record_result


async def executing(tools: WriteTools, store: LiveStore) -> tuple[str, str, Grant]:
    topic_id = await new_topic(tools)
    proposed: Any = await tools.propose_issue(DRAFT, issue(topic_id))
    await store.transition(proposed.proposal_id, ProposalState.APPROVED, approved_by="auto")
    await store.transition(proposed.proposal_id, ProposalState.EXECUTING)
    grant = store.register(
        Grant(
            run_id="run-x",
            kind=RunKind.EXECUTION,
            channel_id=CH,
            proposal_id=proposed.proposal_id,
        )
    )
    return topic_id, proposed.proposal_id, grant


URL = "https://github.com/acme-co/web/issues/7"


async def test_a_matching_result_applies_and_links_the_topic(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id, proposal_id, grant = await executing(tools, store)
    result: Any = await tools.record_result(grant, RecordResultArgs(issue_url=URL))
    assert (result.proposal_id, result.state) == (proposal_id, ProposalState.APPLIED)
    topic = await store.get_topic(topic_id)
    assert topic is not None and (topic.topic.repo, topic.topic.issue_number) == ("acme-co/web", 7)


async def test_a_result_is_recorded_once(tools: WriteTools, store: MemoryStore) -> None:
    _, _, grant = await executing(tools, store)
    first = await tools.record_result(grant, RecordResultArgs(issue_url=URL))
    assert await tools.record_result(grant, RecordResultArgs(issue_url=URL)) == first
    with pytest.raises(
        ToolError, match="is applied, not executing or reconciling; a result is recorded once"
    ):
        await tools.record_result(grant, RecordResultArgs(error="changed my mind"))


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme-co/api/issues/7",
        "https://github.com/acme-co/web/issues/7#issuecomment-1",
    ],
)
async def test_a_result_for_another_repo_or_a_comment_is_refused(
    tools: WriteTools, store: MemoryStore, url: str
) -> None:
    _, _, grant = await executing(tools, store)
    with pytest.raises(ToolError, match="does not match the approved repo"):
        await tools.record_result(grant, RecordResultArgs(issue_url=url))


async def test_an_error_result_fails_the_proposal(tools: WriteTools, store: MemoryStore) -> None:
    _, _, grant = await executing(tools, store)
    result: Any = await tools.record_result(grant, RecordResultArgs(error="rate limited"))
    assert result.state is ProposalState.FAILED


async def reconciling(tools: WriteTools, store: LiveStore) -> tuple[str, Grant]:
    topic_id, proposal_id, grant = await executing(tools, store)
    # A restart interrupted the first execution run; recovery moves the proposal on.
    await store.transition(proposal_id, ProposalState.RECONCILING)
    return topic_id, grant


async def test_a_reconciling_run_that_finds_its_marker_applies(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id, grant = await reconciling(tools, store)
    result: Any = await tools.record_result(grant, RecordResultArgs(issue_url=URL))
    assert result.state is ProposalState.APPLIED
    topic = await store.get_topic(topic_id)
    assert topic is not None and topic.topic.issue_number == 7


async def test_a_reconciling_run_can_give_up(tools: WriteTools, store: MemoryStore) -> None:
    _, grant = await reconciling(tools, store)
    result: Any = await tools.record_result(grant, RecordResultArgs(error="retry limit"))
    assert result.state is ProposalState.FAILED


async def test_only_an_executing_proposal_takes_a_result(
    tools: WriteTools, store: LiveStore
) -> None:
    topic_id = await new_topic(tools)
    proposed: Any = await tools.propose_issue(DRAFT, issue(topic_id))
    grant = store.register(Grant("run-x", RunKind.EXECUTION, CH, proposal_id=proposed.proposal_id))
    with pytest.raises(ToolError, match="is pending, not executing or reconciling"):
        await tools.record_result(grant, RecordResultArgs(issue_url=URL))


async def test_a_proposal_outside_the_grant_reads_as_missing(
    tools: WriteTools, store: LiveStore
) -> None:
    _, proposal_id, _ = await executing(tools, store)
    elsewhere = store.register(Grant("run-y", RunKind.EXECUTION, OTHER, proposal_id=proposal_id))
    missing = store.register(Grant("run-z", RunKind.EXECUTION, CH, proposal_id="prop-none"))
    for grant, pid in ((elsewhere, proposal_id), (missing, "prop-none")):
        with pytest.raises(ToolError, match=f"no proposal {pid}"):
            await tools.record_result(grant, RecordResultArgs(issue_url=URL))


# Registry and the Slack poster


def test_the_write_tools_register_with_their_kinds(store: MemoryStore) -> None:
    specs = checked_registry(write_tools(store, POLICY, Poster()))
    kinds = {name: spec.kinds for name, spec in specs.items()}
    assert kinds == {
        "propose_issue": {RunKind.DRAFTING},
        "propose_update": {RunKind.DRAFTING},
        "link_message": {RunKind.DRAFTING},
        "ask_channel": {RunKind.DRAFTING, RunKind.QUESTION},
        "record_result": {RunKind.EXECUTION},
    }


class FakeClient:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response if response is not None else {"ok": True, "ts": "5.000100"}
        self.error = error

    async def chat_postMessage(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


async def test_the_slack_poster_never_links_names_or_unfurls() -> None:
    client = FakeClient()
    assert await SlackThreadPoster(client).post(CH, ROOT, "Which repo?") == "5.000100"  # type: ignore[arg-type]
    assert client.calls == [
        {
            "channel": CH,
            "thread_ts": ROOT,
            "text": "Which repo?",
            "link_names": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }
    ]


@pytest.mark.parametrize(
    ("client", "problem"),
    [
        (
            FakeClient(error=SlackApiError("no", {"ok": False, "error": "not_in_channel"})),
            "not_in_channel",
        ),
        (FakeClient(error=TimeoutError()), "unreachable: TimeoutError"),
        (FakeClient(response={"ok": True}), "no ts"),
    ],
)
async def test_the_slack_poster_maps_failures(client: FakeClient, problem: str) -> None:
    with pytest.raises(PostError, match=problem):
        await SlackThreadPoster(client).post(CH, ROOT, "x")  # type: ignore[arg-type]


# Through the real gate and server


async def test_over_the_wire_a_refusal_is_a_tool_result_and_kinds_list_their_tools(
    store: MemoryStore, poster: Poster
) -> None:
    from jotflow.whiteboard.tokens import TokenRegistry
    from tests.test_mcp_server import client_for, post

    registry = TokenRegistry()
    drafting = registry.issue(DRAFT, 60)
    execution = registry.issue(
        store.register(Grant("run-x", RunKind.EXECUTION, CH, proposal_id="prop-9")), 60
    )
    specs = write_tools(store, POLICY, poster)
    async with client_for(tokens=registry, tools=specs) as http:
        listed = await post(http, "tools/list", Authorization=f"Bearer {drafting}")
        exec_listed = await post(http, "tools/list", Authorization=f"Bearer {execution}")
        refused = await post(
            http,
            "tools/call",
            {
                "name": "link_message",
                "arguments": {"ts": "9.000100", "decision": "chatter", "confidence": 0.5},
            },
            Authorization=f"Bearer {drafting}",
        )
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {"propose_issue", "propose_update", "link_message", "ask_channel"}
    assert [t["name"] for t in exec_listed.json()["result"]["tools"]] == ["record_result"]
    result = refused.json()["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "message 9.000100 is not in this run's packet"


# The second guard: a run that is no longer running cannot write.


async def test_a_finished_run_cannot_write_even_with_its_token(
    tools: WriteTools, store: LiveStore
) -> None:
    store.ended.add("run-1")
    with pytest.raises(ToolError, match="run run-1 is not running"):
        await tools.link_message(DRAFT, link())


async def test_an_unknown_run_cannot_write(poster: Poster, clock: Clock) -> None:
    plain = MemoryStore(clock=clock)
    tools = WriteTools(plain, POLICY, poster, clock=clock)
    with pytest.raises(ToolError, match="run run-1 is not running"):
        await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))


async def test_a_run_past_its_deadline_but_still_running_may_finish(
    tools: WriteTools, store: LiveStore, clock: Clock
) -> None:
    # The run's deadline (11:30) has passed, but the sweep has not timed it
    # out yet; its last call, here the result of a write, still lands.
    _, _, grant = await executing(tools, store)
    assert clock() > datetime(2026, 10, 18, 11, 30, tzinfo=UTC)
    result: Any = await tools.record_result(grant, RecordResultArgs(issue_url=URL))
    assert result.state is ProposalState.APPLIED


@pytest.mark.parametrize(
    "token_grant",
    [
        # A drafting token whose run is now on another thread.
        Grant("run-p", RunKind.DRAFTING, CH, thread_ts=REPLY),
        # An execution token naming another proposal than its run.
        Grant("run-q", RunKind.EXECUTION, CH, proposal_id="prop-other"),
    ],
)
async def test_a_grant_must_match_its_runs_thread_or_proposal(
    tools: WriteTools, store: LiveStore, token_grant: Grant
) -> None:
    run_grant = (
        Grant(token_grant.run_id, RunKind.DRAFTING, CH, thread_ts=ROOT)
        if token_grant.kind is RunKind.DRAFTING
        else Grant(token_grant.run_id, RunKind.EXECUTION, CH, proposal_id="prop-run")
    )
    store.register(run_grant)
    with pytest.raises(ToolError, match=f"run {token_grant.run_id} is not running"):
        if token_grant.kind is RunKind.DRAFTING:
            await tools.link_message(token_grant, link())
        else:
            await tools.record_result(token_grant, RecordResultArgs(issue_url=URL))


# The policy gate, at the tool (#29): what the policy refuses never becomes a card


class _Cards:
    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def post(self, channel_id: str, thread_ts: str, card: Any) -> str:
        self.posted.append(card)
        return "1.0"

    async def update(self, channel_id: str, ts: str, card: Any) -> None:
        self.posted.append(card)


@pytest.mark.parametrize("tool", ["propose_issue", "propose_update"])
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {"repo": "acme-co/secret"},
            f"repo 'acme-co/secret' is not one of channel {CH}'s repos",
        ),
        ({"label": "wontfix"}, "label list"),
    ],
)
async def test_over_the_wire_a_proposal_outside_the_policy_never_becomes_a_card(
    tools: WriteTools,
    store: LiveStore,
    poster: Poster,
    tool: str,
    change: dict[str, str],
    reason: str,
) -> None:
    from jotflow.orchestrator.card_sync import CardSync
    from jotflow.orchestrator.cards import CardPublisher
    from jotflow.whiteboard.tokens import TokenRegistry
    from tests.test_mcp_server import client_for, post

    topic_id = await new_topic(tools)
    if tool == "propose_issue":
        fields: dict[str, Any] = {}
        if "repo" in change:
            fields["repo"] = change["repo"]
        if "label" in change:
            fields["labels"] = [change["label"]]
        arguments = issue(topic_id, **fields).model_dump(mode="json")
    else:
        await store.set_topic_issue(topic_id, "acme-co/web", 42)
        fields = {"repo": change.get("repo", "acme-co/web")}
        if "label" in change:
            fields["changes"] = {"comment": "x", "add_labels": [change["label"]]}
        arguments = update(topic_id, **fields).model_dump(mode="json")
    registry = TokenRegistry()
    token = registry.issue(DRAFT, 60)
    async with client_for(tokens=registry, tools=write_tools(store, POLICY, poster)) as http:
        reply = await post(
            http,
            "tools/call",
            {"name": tool, "arguments": arguments},
            Authorization=f"Bearer {token}",
        )
    result = reply.json()["result"]
    assert result["isError"] is True
    assert reason in result["content"][0]["text"]
    # Nothing was stored, so no card is due, and nothing reaches Slack.
    assert await store.thread_proposals(CH, ROOT) == []
    assert await store.cards_due() == []
    cards = _Cards()
    sync = CardSync(store, POLICY, CardPublisher(store, cards), _NoNames())
    assert await sync.sync() == 0
    assert cards.posted == [] and poster.posts == []


class _NoNames:
    async def name(self, user_id: str) -> str | None:
        return None


# A channel paused mid-run (#28)


async def test_a_run_in_a_channel_paused_mid_run_writes_nothing_but_its_result(
    tools: WriteTools, store: MemoryStore, poster: Poster
) -> None:
    from jotflow.models import ChannelMode
    from jotflow.whiteboard.write_tools import PAUSED_REASON

    topic_id = await new_topic(tools)  # the run started before the pause
    _, _, exec_grant = await executing(tools, store)
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
    with pytest.raises(ToolError, match="this channel is paused"):
        await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    with pytest.raises(ToolError, match="this channel is paused"):
        await tools.propose_issue(DRAFT, issue(topic_id, title="Something else"))
    with pytest.raises(ToolError, match="this channel is paused"):
        await tools.link_message(DRAFT, link(REPLY, decision="chatter", title=None))
    assert poster.posts == []
    assert "paused" in PAUSED_REASON
    # The write the agent already made on GitHub, approved before the pause, is recorded.
    result: Any = await tools.record_result(exec_grant, RecordResultArgs(issue_url=URL))
    assert result.state is ProposalState.APPLIED


async def test_a_paused_refusal_reaches_the_agent_as_a_tool_error() -> None:
    from jotflow.models import ChannelMode
    from jotflow.whiteboard.tools import ToolError as Refusal

    store = LiveStore(Clock())
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
    await store.record_packet("run-1", [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)])
    tools = WriteTools(store, POLICY, Poster(), clock=Clock(), new_id=Ids())
    with pytest.raises(Refusal) as refused:
        await tools.ask_channel(DRAFT, AskChannelArgs(text="Which repo?"))
    assert refused.value.reason.startswith("this channel is paused")


async def test_the_agents_visibility_report_is_kept_with_the_proposal(
    tools: WriteTools, store: MemoryStore
) -> None:
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(DRAFT, issue(topic_id, repo_visibility="public"))
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and record.proposal.repo_visibility == "public"


async def test_linking_through_the_command_is_what_makes_someone_assignable(
    tools: WriteTools, store: MemoryStore
) -> None:
    from jotflow.orchestrator.identity import LinkCommand
    from jotflow.orchestrator.people import Person

    class People:
        async def person(self, user_id: str) -> Person:
            return Person(user_id)

    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="not the linked GitHub login of a participant"):
        await tools.propose_issue(
            DRAFT, issue(topic_id, participants=["U0BOB"], assignees=["acme-bob"])
        )
    await LinkCommand(store, People()).handle("U0BOB", "link", "acme-bob")
    result: Any = await tools.propose_issue(
        DRAFT, issue(topic_id, participants=["U0BOB"], assignees=["Acme-Bob"])
    )
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and record.proposal.assignees == ["Acme-Bob"]  # as proposed


# issue_links = "none" keeps Slack links out of drafted text (#168)

SLACK_TEXTS = [
    "see https://acme.slack.com/archives/C0EXAMPLE1/p1792310400000100",
    "<https://acme.enterprise.slack.com/archives/C1/p1|the thread>",
    "discussed in app.slack.com/client/T0/C0",
    "open slack://channel?team=T0&id=C0",
]


def _closed_policy() -> Any:
    return POLICY.model_copy(update={"issue_links": "none"})


@pytest.fixture
def closed(tools: WriteTools, store: MemoryStore, poster: Poster, clock: Clock) -> WriteTools:
    """The write tools, with the packet recorded, in a channel set to issue_links = none."""
    return WriteTools(store, _closed_policy(), poster, clock=clock, new_id=Ids())


@pytest.mark.parametrize("text", SLACK_TEXTS)
@pytest.mark.parametrize("field", ["title", "body"])
async def test_with_issue_links_none_a_slack_link_in_an_issue_is_refused(
    closed: WriteTools, store: MemoryStore, field: str, text: str
) -> None:
    tools = closed
    topic_id = await new_topic(tools)
    with pytest.raises(ToolError, match="keeps Slack out of GitHub") as refused:
        await tools.propose_issue(DRAFT, issue(topic_id, **{field: f"Export: {text}"[:200]}))
    assert str(refused.value) == wt.NO_SLACK_LINKS
    assert await store.thread_proposals(CH, ROOT) == []


@pytest.mark.parametrize("change", ["body", "comment"])
async def test_with_issue_links_none_a_slack_link_in_an_update_is_refused(
    closed: WriteTools, store: MemoryStore, change: str
) -> None:
    tools = closed
    topic_id = await new_topic(tools)
    await store.set_topic_issue(topic_id, "acme-co/web", 42)
    changes = {change: f"Also: {SLACK_TEXTS[0]}"}
    with pytest.raises(ToolError, match="keeps Slack out of GitHub"):
        await tools.propose_update(DRAFT, update(topic_id, changes=changes))
    assert await store.thread_proposals(CH, ROOT) == []


async def test_with_issue_links_none_ordinary_text_about_slack_is_fine(
    closed: WriteTools,
) -> None:
    tools = closed
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(
        DRAFT, issue(topic_id, title="Slack sign-in fails", body="The Slack button on notslack.com")
    )
    assert result.state is ProposalState.PENDING


async def test_with_issue_links_on_a_slack_link_is_allowed(tools: WriteTools) -> None:
    topic_id = await new_topic(tools)
    result: Any = await tools.propose_issue(DRAFT, issue(topic_id, body=f"See {SLACK_TEXTS[0]}"))
    assert result.state is ProposalState.PENDING


async def test_over_the_wire_the_refusal_is_a_tool_result_the_agent_can_act_on(
    closed: WriteTools, store: LiveStore, poster: Poster
) -> None:
    from jotflow.whiteboard.tokens import TokenRegistry
    from tests.test_mcp_server import client_for, post

    tools = closed
    topic_id = await new_topic(tools)
    registry = TokenRegistry()
    token = registry.issue(DRAFT, 60)
    arguments = issue(topic_id, body=f"See {SLACK_TEXTS[0]}").model_dump(mode="json")
    specs = write_tools(store, _closed_policy(), poster)
    async with client_for(tokens=registry, tools=specs) as http:
        reply = await post(
            http,
            "tools/call",
            {"name": "propose_issue", "arguments": arguments},
            Authorization=f"Bearer {token}",
        )
    result = reply.json()["result"]
    assert result["isError"] is True and result["content"][0]["text"] == wt.NO_SLACK_LINKS
