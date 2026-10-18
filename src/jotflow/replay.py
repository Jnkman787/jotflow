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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from jotflow.models import RunKind, RunStatus
from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.packet import SlackWorkSource
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
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolError, ToolSpec
from jotflow.whiteboard.write_tools import write_tools

# Replay runs at a fixed time, so every run of a fixture is identical.
REPLAY_START = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
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


class _FixtureReader:
    """The two Slack reads the live work packet makes, answered from the fixture.

    Replay builds each run's thread with the real
    :class:`~jotflow.orchestrator.packet.SlackWorkSource`, so ``skip:``, keyword
    stripping, hints, mentions, and related excerpts follow the live rules.
    """

    def __init__(self, thread: SavedThread, accepted: Sequence[MessageEvent]) -> None:
        self._thread = thread
        self._accepted = accepted

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        thread = [m for m in self._accepted if m.channel == channel and m.root == ts]
        thread.sort(key=lambda m: (m.ts != ts, m.ts))  # the parent first, as Slack does
        messages = [_slack_message(m) for m in thread][:limit]
        return {"ok": True, "messages": messages, "has_more": False}

    async def user(self, user_id: str) -> Mapping[str, Any]:
        found = self._thread.users.get(user_id)
        name = found.name if found else user_id
        return {"ok": True, "user": {"id": user_id, "profile": {"display_name": name}}}


def _slack_message(message: MessageEvent) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "message", "ts": message.ts, "text": message.text}
    for key in ("thread_ts", "user", "bot_id", "subtype"):
        value = getattr(message, key)
        if value is not None:
            data[key] = value
    return data


@dataclass
class _Replay:
    thread: SavedThread
    store: Store
    clock: _Clock
    specs: dict[str, ToolSpec]
    poster: _Poster
    topics: dict[str, str] = field(default_factory=dict)  # key:<name> or fixture id -> store id
    accepted: list[MessageEvent] = field(default_factory=list)  # messages ingest accepted

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
        # Reports name messages by ts only, so no error text that could quote
        # a message or an argument reaches them.
        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            fields = ", ".join(".".join(str(p) for p in e["loc"]) or "-" for e in exc.errors())
            raise ReplayError(f"{tool} got invalid arguments: {fields}") from None
        try:
            result: BaseModel = await spec.handler(grant, parsed)
        except ToolError as exc:
            # Tool reasons are written in code and never quote Slack text.
            raise ReplayError(f"{tool} refused the scripted call: {exc.reason}") from None
        except Exception as exc:
            raise ReplayError(f"{tool} failed: {type(exc).__name__}") from None
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


def scripted_call(
    call: Any, resolve: Callable[[str | None], str | None], participants: list[str]
) -> tuple[str, dict[str, Any]]:
    """A fixture's scripted call as the tool name and arguments a client sends."""
    if isinstance(call, LinkMessageCall):
        decision = call.arguments
        return "link_message", {
            "ts": decision.message.ts,
            "decision": decision.decision,
            "topic_id": resolve(decision.topic_id),
            "title": call.title,
            "confidence": decision.confidence,
        }
    if isinstance(call, ProposeIssueCall | ProposeUpdateCall):
        tool = "propose_issue" if isinstance(call, ProposeIssueCall) else "propose_update"
        arguments = call.arguments.model_dump(mode="json", exclude_none=True)
        arguments["topic_id"] = resolve(call.arguments.topic_id)
        arguments["participants"] = participants
        return tool, arguments
    if isinstance(call, AskChannelCall):
        return "ask_channel", call.arguments.model_dump()
    raise ReplayError(f"unknown scripted call {call!r}")  # pragma: no cover - four types only


async def _make_call(replay: _Replay, grant: Grant, call: Any, participants: list[str]) -> None:
    tool, arguments = scripted_call(call, replay.resolve, participants)
    result = await replay.call(grant, tool, arguments)
    if isinstance(call, LinkMessageCall) and call.topic_key:
        replay.topics[TOPIC_KEY_PREFIX + call.topic_key] = result.model_dump()["topic_id"]


async def _compare(replay: _Replay, accepted: list[str], ignored: dict[str, str]) -> list[Check]:
    expected = replay.thread.expected
    checks = [
        _equal("ingest accepted", accepted, list(expected.accepted)),
        _equal("ingest ignored", ignored, dict(expected.ignored)),
    ]
    # Attaching a re: message to its topic is #56's; until then those messages are
    # set aside here. The keywords check below still checks that they say re:.
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
    found = {
        m.ts: keyword for m in replay.accepted if (keyword := split_keyword(m.text)[0]) is not None
    }
    checks.append(_equal("keywords", found, dict(expected.keywords)))
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


@dataclass
class Prepared:
    """A fixture ingested and seeded, with its tools, ready for runs."""

    state: _Replay
    lifecycle: RunLifecycle
    registry: TokenRegistry
    report: ReplayReport
    accepted: list[str]
    ignored: dict[str, str]


async def prepare(thread: SavedThread, *, store: Store | None = None) -> Prepared:
    """Ingest ``thread``, seed the store, and build the real tools over it."""
    clock = _Clock()
    active = store or MemoryStore(clock=clock)
    poster = _Poster()
    accepted, ignored, messages = _ingest(thread)
    bot = thread.workspace.bot
    source = SlackWorkSource(
        _FixtureReader(thread, messages), active, bot=BotIdentity(bot.user_id, bot.bot_id)
    )
    specs = {
        spec.name: spec
        for spec in (
            *read_tools(active, thread.policy, source, clock=clock),
            *write_tools(active, thread.policy, poster, clock=clock),
        )
    }
    state = _Replay(thread, active, clock, specs, poster, accepted=messages)
    registry = TokenRegistry(clock=clock.mono)
    lifecycle = RunLifecycle(active, registry, clock=clock)
    report = ReplayReport(thread.name, thread.description)
    await _seed(state)
    return Prepared(state, lifecycle, registry, report, accepted, ignored)


async def finish(prepared: Prepared) -> ReplayReport:
    """Compare the store with the fixture's expected outcome, and add the invariants."""
    state = prepared.state
    prepared.report.checks += await _compare(state, prepared.accepted, prepared.ignored)
    prepared.report.checks += await invariants(state)
    return prepared.report


async def replay(thread: SavedThread, *, store: Store | None = None) -> ReplayReport:
    """Run ``thread`` through the pipeline and compare it with its expected outcome."""
    prepared = await prepare(thread, store=store)
    state, report = prepared.state, prepared.report
    try:
        await _run_script(state, prepared.lifecycle, prepared.registry, state.accepted)
    except ReplayError as exc:
        report.checks.append(Check("script", False, str(exc)))
    else:
        report.checks.append(Check("script", True, f"{len(thread.agent)} run(s) completed"))
    # Compare even after a refused call, so the report shows what differs.
    return await finish(prepared)


async def invariants(replay: _Replay) -> list[Check]:
    """Rules every run must keep, whatever the fixture expects (#23)."""
    channel = _channel(replay.thread)
    logins = {u.github.casefold() for u in replay.thread.users.values() if u.github}
    from_chatter: list[str] = []
    unlinked: list[str] = []
    for root in sorted({run.thread_ts for run in replay.thread.agent}):
        for record in await replay.store.thread_proposals(channel, root):
            draft = record.proposal
            for source in draft.sources:
                link = await replay.store.get_message(channel, source.ts)
                if link is not None and link.chatter:
                    from_chatter.append(source.ts)
            assignees = (
                draft.assignees if draft.kind == "create_issue" else draft.changes.add_assignees
            )
            unlinked += [a for a in assignees if a.casefold() not in logins]
    return [
        Check(
            "no proposal from chatter",
            not from_chatter,
            f"chatter used as a source: {', '.join(from_chatter)}" if from_chatter else "none",
        ),
        Check(
            "assignees are linked",
            not unlinked,
            f"not linked: {', '.join(unlinked)}" if unlinked else "all linked",
        ),
    ]
