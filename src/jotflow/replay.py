"""Replay a saved thread through JotFlow's real pipeline (#17, D4).

A saved thread (``jotflow-thread/1``, see ``testing/threads/README.md``) runs
through the real ingest filter, the store, the run lifecycle and its MCP
tokens, and the real read and write tools. The fixture's scripted fake agent
stands in for a real one, and nothing touches Slack or the network. The
result is compared with the fixture's ``expected`` outcome.

The report names messages by ``ts`` only, never by what they say.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from jotflow.models import RunKind, RunStatus, UntrustedText
from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.store import MemoryStore, Store
from jotflow.testing.threads import (
    TOPIC_KEY_PREFIX,
    AskChannelCall,
    LinkMessageCall,
    MessageEvent,
    ProposeIssueCall,
    ProposeUpdateCall,
    ReactionEvent,
    SavedThread,
)
from jotflow.whiteboard.read_tools import (
    RelatedExcerpt,
    ThreadMessage,
    WorkThread,
    read_tools,
)
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolSpec
from jotflow.whiteboard.write_tools import write_tools

# Replay runs at a fixed time, so every run of a fixture is identical.
REPLAY_START = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
NOT_RUN_KEYWORDS = (
    "not checked: re: and skip: are applied by keyword parsing, which arrives with #20"
)

__all__ = ["Check", "ReplayError", "ReplayReport", "replay"]


class ReplayError(Exception):
    """The fixture's script could not run, for example a tool refused a call."""


@dataclass(frozen=True)
class Check:
    """One comparison with ``expected``. ``ok`` is None for a check not run yet."""

    name: str
    ok: bool | None
    detail: str


@dataclass
class ReplayReport:
    thread: str
    description: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok is not False for check in self.checks)

    def render(self) -> str:
        marks = {True: "ok  ", False: "FAIL", None: "--  "}
        lines = [f"Replaying {self.thread}: {self.description}", ""]
        lines += [f"  {marks[c.ok]}  {c.name}: {c.detail}" for c in self.checks]
        lines += [
            "",
            "Matches the expected outcome." if self.ok else "Does not match the expected outcome.",
        ]
        return "\n".join(lines)


class _Clock:
    def __init__(self) -> None:
        self.now = REPLAY_START

    def __call__(self) -> datetime:
        return self.now

    def mono(self) -> float:
        return (self.now - REPLAY_START).total_seconds()


class _Poster:
    """Records ask_channel posts instead of sending them."""

    def __init__(self) -> None:
        self.threads: list[str] = []

    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        self.threads.append(thread_ts)
        return f"1792310999.{len(self.threads):06d}"


class _FixtureSource:
    """Serves each thread's accepted messages from the fixture, as Slack would."""

    def __init__(self, thread: SavedThread, accepted: Sequence[MessageEvent]) -> None:
        self._thread = thread
        self._accepted = accepted

    async def thread(self, channel_id: str, thread_ts: str) -> WorkThread:
        messages = [
            ThreadMessage(
                ts=m.ts,
                user_id=m.user,
                author=UntrustedText(text=self._name(m.user)),
                text=UntrustedText(text=m.text),
            )
            for m in self._accepted
            if m.channel == channel_id and m.root == thread_ts
        ]
        return WorkThread(channel_id=channel_id, thread_ts=thread_ts, messages=messages)

    async def related(self, channel_id: str, thread_ts: str, topics: Any) -> list[RelatedExcerpt]:
        return []

    def _name(self, user: str | None) -> str:
        if user is None:
            return "unknown"
        found = self._thread.users.get(user)
        return found.name if found else user


@dataclass
class _Replay:
    thread: SavedThread
    store: Store
    clock: _Clock
    specs: dict[str, ToolSpec]
    poster: _Poster
    topics: dict[str, str] = field(default_factory=dict)  # key:<name> or fixture id -> store id

    def resolve(self, topic: str | None) -> str | None:
        if topic is None:
            return None
        if topic not in self.topics:
            raise ReplayError(f"the script names topic {topic} before it exists")
        return self.topics[topic]

    async def call(self, grant: Grant, tool: str, arguments: dict[str, Any]) -> BaseModel:
        spec = self.specs[tool]
        if grant.kind not in spec.kinds:
            raise ReplayError(f"a {grant.kind.value} run may not call {tool}")
        try:
            result: BaseModel = await spec.handler(
                grant, spec.input_model.model_validate(arguments)
            )
        except Exception as exc:
            raise ReplayError(f"{tool} refused the scripted call: {exc}") from None
        return result


def _ingest(thread: SavedThread) -> tuple[list[str], dict[str, str], list[MessageEvent]]:
    bot = thread.workspace.bot
    events = EventFilter(thread.policy.channels, BotIdentity(bot.user_id, bot.bot_id))
    accepted: list[str] = []
    ignored: dict[str, str] = {}
    messages: list[MessageEvent] = []
    for event in thread.events:
        decision = events.classify(event.model_dump(exclude_none=True))
        key = f"{event.item.ts}:{event.reaction}" if isinstance(event, ReactionEvent) else event.ts
        if isinstance(decision, Accepted):
            accepted.append(key)
            if isinstance(event, MessageEvent):
                messages.append(event)
        else:
            ignored[key] = decision.reason
    return accepted, ignored, messages


async def _seed(replay: _Replay) -> None:
    thread, store = replay.thread, replay.store
    for user_id, user in thread.users.items():
        if user.github:
            await store.link_identity(user_id, user.github)
    for topic in thread.existing_topics:
        record = await store.create_topic(topic.channel_id, topic.title)
        if topic.repo and topic.issue_number:
            await store.set_topic_issue(record.topic.id, topic.repo, topic.issue_number)
        for ref in topic.messages:
            await store.link_message(ref, record.topic.id)
        replay.topics[topic.id] = record.topic.id


def _participants(thread: SavedThread, accepted: Sequence[MessageEvent], root: str) -> list[str]:
    bot = thread.workspace.bot.user_id
    seen: list[str] = []
    for message in accepted:
        user = message.user
        if message.root == root and user and user != bot and user not in seen:
            seen.append(user)
    return seen


async def _run_script(
    replay: _Replay, lifecycle: RunLifecycle, registry: TokenRegistry, accepted: list[MessageEvent]
) -> None:
    max_running = replay.thread.policy.agent.max_concurrent_runs
    for run in replay.thread.agent:
        await replay.store.enqueue_run(RunKind.DRAFTING, run.channel, run.thread_ts)
        started = await lifecycle.start_next(max_running=max_running)
        if started is None:
            raise ReplayError(f"the run for thread {run.thread_ts} did not start")
        grant = registry.check(started.token)
        if grant is None:  # pragma: no cover - the token was issued a moment ago
            raise ReplayError("the run's token was refused")
        try:
            await replay.call(grant, "get_work", {})
            participants = _participants(replay.thread, accepted, run.thread_ts)
            for call in run.calls:
                await _make_call(replay, grant, call, participants)
        except ReplayError:
            await lifecycle.finish(started.run.id, RunStatus.FAILED, error="scripted call refused")
            raise
        await lifecycle.finish(started.run.id, RunStatus.SUCCEEDED)


async def _make_call(replay: _Replay, grant: Grant, call: Any, participants: list[str]) -> None:
    if isinstance(call, LinkMessageCall):
        decision = call.arguments
        result = await replay.call(
            grant,
            "link_message",
            {
                "ts": decision.message.ts,
                "decision": decision.decision,
                "topic_id": replay.resolve(decision.topic_id),
                "title": call.title,
                "confidence": decision.confidence,
            },
        )
        if call.topic_key:
            replay.topics[TOPIC_KEY_PREFIX + call.topic_key] = result.model_dump()["topic_id"]
    elif isinstance(call, ProposeIssueCall | ProposeUpdateCall):
        tool = "propose_issue" if isinstance(call, ProposeIssueCall) else "propose_update"
        arguments = call.arguments.model_dump(mode="json")
        arguments["topic_id"] = replay.resolve(call.arguments.topic_id)
        arguments["participants"] = participants
        await replay.call(grant, tool, arguments)
    elif isinstance(call, AskChannelCall):
        await replay.call(grant, "ask_channel", call.arguments.model_dump())
    else:  # pragma: no cover - the fixture model admits only the four call types
        raise ReplayError(f"unknown scripted call {call!r}")


async def _compare(replay: _Replay, accepted: list[str], ignored: dict[str, str]) -> list[Check]:
    expected = replay.thread.expected
    checks = [
        _equal("ingest accepted", accepted, list(expected.accepted)),
        _equal("ingest ignored", ignored, dict(expected.ignored)),
    ]
    attached_by_keyword = {ts for ts, word in expected.keywords.items() if word == "re"}

    topic_problems: list[str] = []
    for want in expected.topics:
        topic_id = replay.topics.get(want.topic)
        record = await replay.store.get_topic(topic_id) if topic_id else None
        if record is None:
            topic_problems.append(f"{want.topic} was not created")
            continue
        # Only this thread's messages: an existing topic already holds older ones.
        in_thread = replay.thread.messages
        got = {m.ts for m in record.topic.messages if m.ts in in_thread}
        wanted = set(want.messages) - attached_by_keyword
        if got - attached_by_keyword != wanted:
            topic_problems.append(f"{want.topic} has {sorted(got)}, expected {sorted(wanted)}")
    checks.append(
        Check(
            "topics",
            not topic_problems,
            "; ".join(topic_problems) or f"{len(expected.topics)} as expected",
        )
    )

    channel = _channel(replay.thread)
    wrong_chatter = []
    for ts in expected.chatter:
        link = await replay.store.get_message(channel, ts)
        if link is None or not link.chatter:
            wrong_chatter.append(ts)
    checks.append(
        Check(
            "chatter",
            not wrong_chatter,
            f"not marked chatter: {', '.join(wrong_chatter)}"
            if wrong_chatter
            else f"{len(expected.chatter)} as expected",
        )
    )

    by_store_id = {v: k for k, v in replay.topics.items()}
    got_proposals: Counter[tuple[str, str, str, int | None]] = Counter()
    for root in sorted({run.thread_ts for run in replay.thread.agent}):
        for proposal in await replay.store.thread_proposals(channel, root):
            draft = proposal.proposal
            topic = by_store_id.get(draft.topic_id or "", draft.topic_id or "")
            number = getattr(draft, "issue_number", None)
            got_proposals[(draft.kind, draft.repo.casefold(), topic, number)] += 1
    want_proposals = Counter(
        (p.kind, p.repo.casefold(), p.topic, p.issue_number) for p in expected.proposals
    )
    checks.append(_counted("proposals", got_proposals, want_proposals))
    checks.append(_equal("asks", replay.poster.threads, list(expected.asks)))
    if any(word in {"re", "skip"} for word in expected.keywords.values()):
        checks.append(Check("keywords", None, NOT_RUN_KEYWORDS))
    return checks


def _channel(thread: SavedThread) -> str:
    return next(iter(thread.policy.channels))


def _equal(name: str, got: Any, want: Any) -> Check:
    if got == want:
        return Check(name, True, f"{len(want)} as expected")
    return Check(name, False, f"got {got}, expected {want}")


def _counted(name: str, got: Counter[Any], want: Counter[Any]) -> Check:
    if got == want:
        return Check(name, True, f"{sum(want.values())} as expected")
    missing = list((want - got).elements())
    extra = list((got - want).elements())
    return Check(name, False, f"missing {missing}, unexpected {extra}")


async def replay(thread: SavedThread, *, store: Store | None = None) -> ReplayReport:
    """Run ``thread`` through the pipeline and compare it with its expected outcome."""
    clock = _Clock()
    active = store or MemoryStore(clock=clock)
    poster = _Poster()
    accepted, ignored, messages = _ingest(thread)
    source = _FixtureSource(thread, messages)
    specs = {
        spec.name: spec
        for spec in (
            *read_tools(active, thread.policy, source, clock=clock),
            *write_tools(active, thread.policy, poster, clock=clock),
        )
    }
    state = _Replay(thread, active, clock, specs, poster)
    registry = TokenRegistry(clock=clock.mono)
    lifecycle = RunLifecycle(active, registry, clock=clock)
    report = ReplayReport(thread.name, thread.description)
    await _seed(state)
    try:
        await _run_script(state, lifecycle, registry, messages)
    except ReplayError as exc:
        report.checks.append(Check("script", False, str(exc)))
    else:
        report.checks.append(Check("script", True, f"{len(thread.agent)} run(s) completed"))
    # Compare even after a refused call, so the report shows what differs.
    report.checks += await _compare(state, accepted, ignored)
    return report
