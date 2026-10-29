"""Replay a saved thread through JotFlow's real pipeline (#17, D4).

A saved thread (``jotflow-thread/1``, see ``testing/threads/README.md``) runs
through the real ingest filter, the store, the run lifecycle and its MCP
tokens, and the real read and write tools. The fixture's scripted fake agent
stands in for a real one, and nothing touches Slack or the network. The
result is compared with the fixture's ``expected`` outcome.

The report names messages by ``ts`` only, never by what they say.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from jotflow.models import MessageRef, ProposalRecord, RunKind, RunStatus
from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter
from jotflow.orchestrator.keyword_handler import KeywordHandler, Person
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.packet import SlackWorkSource
from jotflow.orchestrator.questions import QuestionHandler, is_ask
from jotflow.orchestrator.runs import RunLifecycle, StartedRun
from jotflow.store import MemoryStore, Store
from jotflow.testing.threads import (
    FIXTURE_LISTED_AT,
    TOPIC_KEY_PREFIX,
    AgentRun,
    AnswerCall,
    AskChannelCall,
    LinkMessageCall,
    ListRepoDocsCall,
    MessageEvent,
    ProposeIssueCall,
    ProposeUpdateCall,
    ReactionEvent,
    ReadRepoDocCall,
    ReviseProposalCall,
    SavedThread,
    fixture_catalog,
    invented_commands,
    invented_links,
    invented_numbers,
    play_order,
)
from jotflow.whiteboard.doc_tools import READ_TOOL, DocReads, doc_tools
from jotflow.whiteboard.read_tools import LIVE_PROPOSAL_STATES, read_tools
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolError, ToolSpec
from jotflow.whiteboard.write_tools import write_tools

# Replay runs at a fixed time, so every run of a fixture is identical.
REPLAY_START = FIXTURE_LISTED_AT
__all__ = ["Check", "ReplayError", "ReplayReport", "replay"]


class ReplayError(Exception):
    """The fixture's script could not run, for example a tool refused a call."""


@dataclass(frozen=True)
class Check:
    """One comparison with ``expected``."""

    name: str
    ok: bool
    detail: str


@dataclass
class ReplayReport:
    thread: str
    description: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def render(self) -> str:
        marks = {True: "ok  ", False: "FAIL"}
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
        # Each answer as JotFlow would post it: (thread, posted ts, text) (#57, #270).
        self.answers: list[tuple[str, str, str]] = []

    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        self.threads.append(thread_ts)
        return f"1792310999.{len(self.threads):06d}"

    async def post_plain(self, channel_id: str, thread_ts: str, text: str) -> str:
        """A question run's answer (#57): recorded, never sent."""
        ts = f"1792311999.{len(self.answers) + 1:06d}"
        self.answers.append((thread_ts, ts, text))
        return ts


# The fictional workspace's address, for permalinks in planned issue bodies.
FIXTURE_WORKSPACE_URL = "https://acme-co.slack.com/"


class FixtureAttribution:
    """Who wrote what and their names, from the fixture, for execution footers (#31)."""

    def __init__(self, thread: SavedThread) -> None:
        self._thread = thread

    async def authors(self, refs: Sequence[MessageRef]) -> set[str]:
        found: set[str] = set()
        for ref in refs:
            message = self._thread.messages.get(ref.ts)
            if message is None:
                raise LookupError(f"message {ref.ts} was not found")
            if message.user and message.user != self._thread.workspace.bot.user_id:
                found.add(message.user)
        return found

    async def name(self, user_id: str) -> str | None:
        user = self._thread.users.get(user_id)
        return user.name if user else None

    async def workspace_url(self) -> str | None:
        return FIXTURE_WORKSPACE_URL


class _FixtureReader:
    """The two Slack reads the live work packet makes, answered from the fixture.

    Replay builds each run's thread with the real
    :class:`~jotflow.orchestrator.packet.SlackWorkSource`, so ``skip:``, keyword
    stripping, hints, mentions, and related excerpts follow the live rules.
    """

    def __init__(self, thread: SavedThread, accepted: Sequence[MessageEvent]) -> None:
        self._thread = thread
        self._accepted = accepted
        # The last message the current run sees; later ones aren't posted yet (#304).
        self.until: str | None = None

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        thread = [
            m
            for m in self._accepted
            if m.channel == channel
            and m.root == ts
            and (self.until is None or float(m.ts) <= float(self.until))
        ]
        thread.sort(key=lambda m: (m.ts != ts, m.ts))  # the parent first, as Slack does
        messages = [_slack_message(m) for m in thread][:limit]
        return {"ok": True, "messages": messages, "has_more": False}

    async def user(self, user_id: str) -> Mapping[str, Any]:
        found = self._thread.users.get(user_id)
        name = found.name if found else user_id
        return {"ok": True, "user": {"id": user_id, "profile": {"display_name": name}}}


class _FixtureSlack:
    """The Slack calls keyword handling makes, answered from the fixture.

    Reactions and replies are recorded, never sent.
    """

    def __init__(self, thread: SavedThread) -> None:
        self._thread = thread
        self.reactions: list[tuple[str, str]] = []
        self.replies: list[str] = []
        self.whispers: list[tuple[str, str]] = []  # (user, text): questions' private replies

    async def own_team(self) -> str | None:
        return self._thread.workspace.team_id

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        self.whispers.append((user_id, text))

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self.reactions.append((ts, name))

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        self.replies.append(thread_ts)

    async def person(self, user_id: str) -> Person:
        found = self._thread.users.get(user_id)
        if found is None:
            return Person(user_id)
        return Person(user_id, guest=found.guest, external=found.external)


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
    attribution: FixtureAttribution | None = None
    slack: _FixtureSlack | None = None
    reader: _FixtureReader | None = None
    removed: list[int] = field(default_factory=list)  # what each answer's rewrite removed
    exact: bool = True  # the scripted agent: its answers' exact words are checked
    run_ids: list[str] = field(default_factory=list)  # the scripted runs, in order

    def resolve(self, topic: str | None) -> str | None:
        if topic is None:
            return None
        if topic not in self.topics:
            raise ReplayError(f"the script names topic {topic} before it exists")
        return self.topics[topic]

    async def call(self, grant: Grant, tool: str, arguments: dict[str, Any]) -> BaseModel:
        spec = self.specs.get(tool)
        if spec is None:
            # Such as a doc tool when no repo sets read_paths (#209).
            raise ReplayError(f"there is no {tool} tool with this fixture's policy")
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


class FixtureDocs:
    """A fixture's repo files, served as the drafting runner's doc endpoints would."""

    def __init__(self, docs: Mapping[str, Mapping[str, str]]) -> None:
        self._docs = {repo.casefold(): files for repo, files in docs.items()}

    async def docs_list(self, repo: str) -> _Listed:
        return _Listed(paths=sorted(self._docs.get(repo.casefold(), {})))

    async def docs_read(self, repo: str, path: str) -> _Fetched:
        text = self._docs.get(repo.casefold(), {}).get(path)
        if text is None:
            raise ReplayError("the fixture has no such doc")
        # A stand-in for git's blob sha: a fixture has no repo.
        sha = hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()
        return _Fetched(path=path, sha=sha, text=text)


@dataclass(frozen=True)
class _Listed:
    paths: list[str]
    truncated: bool = False


@dataclass(frozen=True)
class _Fetched:
    path: str
    sha: str
    text: str
    truncated: bool = False


def _doc_keys(rows: Sequence[str]) -> list[str]:
    keys = []
    for row in rows:
        data = json.loads(row)
        keys.append(f"{data['repo']}:{data['path']}")
    return keys


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


def _questions(replay: _Replay) -> QuestionHandler:
    slack = replay.slack or _FixtureSlack(replay.thread)
    return QuestionHandler(
        replay.store,
        replay.thread.policy,
        slack,
        replay.thread.workspace.bot.user_id,
        clock=replay.clock,
        monotonic=replay.clock.mono,
    )


async def _apply_keywords(replay: _Replay, accepted: Sequence[str]) -> None:
    """Questions, then re: and skip:, with the live handlers, before any run (#56, #57).

    As live, an ``ask:`` the question handler takes, queued or refused, gets no
    keyword handling, and its question run is queued here, not by the script. A
    bot mention goes on to keywords and drafting too (#329).
    """
    slack = replay.slack or _FixtureSlack(replay.thread)
    handler = KeywordHandler(replay.store, replay.thread.policy, slack)
    questions = _questions(replay)
    wanted = set(accepted)
    for event in replay.thread.events:
        if isinstance(event, ReactionEvent):
            if f"{event.item.ts}:{event.reaction}" in wanted:
                await handler.on_reaction(event.model_dump(exclude_none=True))
        elif event.ts in wanted:
            data = event.model_dump(exclude_none=True)
            await questions.on_message(data)
            if is_ask(event.text or ""):
                continue
            await handler.on_message(data)


def _participants(thread: SavedThread, accepted: Sequence[MessageEvent], root: str) -> list[str]:
    bot = thread.workspace.bot.user_id
    seen: list[str] = []
    for message in accepted:
        user = message.user
        if message.root == root and user and user != bot and user not in seen:
            seen.append(user)
    return seen


async def run_script(
    replay: _Replay, lifecycle: RunLifecycle, registry: TokenRegistry, accepted: list[MessageEvent]
) -> None:
    """Play the fixture's scripted runs in-process: question runs, then drafting (#270)."""
    for _, run in play_order(replay.thread):
        started = await start_scripted(replay, lifecycle, run)
        grant = registry.check(started.token)
        if grant is None:  # pragma: no cover - the token was issued a moment ago
            raise ReplayError("the run's token was refused")
        replay.run_ids.append(started.run.id)
        if replay.reader is not None:
            replay.reader.until = run.sees_until
        try:
            await replay.call(grant, "get_work", {})
            participants = _participants(replay.thread, accepted, run.thread_ts)
            for call in run.calls:
                await _make_call(replay, grant, call, participants)
        except ReplayError:
            await lifecycle.finish(started.run.id, RunStatus.FAILED, error="scripted call refused")
            raise
        await lifecycle.finish(started.run.id, RunStatus.SUCCEEDED)


async def start_scripted(replay: _Replay, lifecycle: RunLifecycle, run: AgentRun) -> StartedRun:
    """Start the run a fixture scripts. A question run must already be queued.

    The question handler queued it, as live, so a script for a question JotFlow
    refused, such as a guest's, fails here. The started run's kind and thread are
    checked, so a misordering fails rather than being misattributed.
    """
    kind = RunKind.QUESTION if run.kind == "question" else RunKind.DRAFTING
    if kind is RunKind.DRAFTING:
        await replay.store.enqueue_run(kind, run.channel, run.thread_ts)
    max_running = replay.thread.policy.agent.max_concurrent_runs
    started = await lifecycle.start_next(max_running=max_running, kinds={kind})
    if started is None:
        if kind is RunKind.QUESTION:
            raise ReplayError(f"the question run for thread {run.thread_ts} was not queued")
        raise ReplayError(f"the run for thread {run.thread_ts} did not start")
    if started.run.thread_ts != run.thread_ts or started.run.channel_id != run.channel:
        await lifecycle.finish(started.run.id, RunStatus.FAILED, error="not the scripted run")
        raise ReplayError(
            f"the {kind.value} run started for thread {started.run.thread_ts}, not {run.thread_ts}"
        )
    return started


async def _live_proposal(replay: _Replay, topic_id: str) -> ProposalRecord | None:
    """The topic's proposal still in play, as the packet's ``pending`` would give it."""
    record = await replay.store.get_topic(topic_id)
    if record is None:
        return None
    threads = {ref.thread_ts or ref.ts for ref in record.topic.messages}
    for thread in sorted(threads):
        for proposal in await replay.store.thread_proposals(_channel(replay.thread), thread):
            if proposal.proposal.topic_id == topic_id and proposal.state in LIVE_PROPOSAL_STATES:
                return proposal
    return None


def scripted_call(
    call: Any,
    resolve: Callable[[str | None], str | None],
    participants: list[str],
    pending_of: Callable[[str], tuple[str, int] | None] = lambda _: None,
) -> tuple[str, dict[str, Any]]:
    """A fixture's scripted call as the tool name and arguments a client sends.

    ``pending_of`` maps a topic id to its pending proposal's id and revision, for a
    scripted revision (#304): replay reads it from the store, the stand-in agent from
    its packet.
    """
    if isinstance(call, ReviseProposalCall):
        revise = call.arguments
        topic = resolve(revise.topic_id) or ""
        found = pending_of(topic)
        if found is None:
            raise ReplayError(f"topic {revise.topic_id} has no pending draft to revise")
        arguments: dict[str, Any] = {
            "proposal_id": found[0],
            "base_revision": found[1],
            "sources": [s.model_dump(mode="json", exclude_none=True) for s in revise.sources],
            "confidence": revise.confidence,
        }
        if revise.issue is not None:
            arguments["issue"] = revise.issue.model_dump(mode="json")
        if revise.changes is not None:
            arguments["changes"] = revise.changes.model_dump(mode="json")
        return "revise_proposal", arguments
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
    if isinstance(call, AnswerCall):
        return "answer", call.arguments.model_dump()
    if isinstance(call, ListRepoDocsCall):
        return "list_repo_docs", {"repo": call.arguments.repo}
    if isinstance(call, ReadRepoDocCall):
        return "read_repo_doc", {"repo": call.arguments.repo, "path": call.arguments.path}
    raise ReplayError(f"unknown scripted call {call!r}")  # pragma: no cover - seven types only


async def _make_call(replay: _Replay, grant: Grant, call: Any, participants: list[str]) -> None:
    pending: dict[str, tuple[str, int]] = {}
    if isinstance(call, ReviseProposalCall):
        topic = replay.resolve(call.arguments.topic_id) or ""
        if (record := await _live_proposal(replay, topic)) is not None:
            pending[topic] = (record.id, record.revision)
    tool, arguments = scripted_call(call, replay.resolve, participants, pending.get)
    expected = getattr(call, "expect_error", None)
    if expected is not None:
        # A call the script expects to be refused, such as a title off its pattern.
        try:
            await replay.call(grant, tool, arguments)
        except ReplayError as exc:
            if expected not in str(exc):
                raise
            return
        raise ReplayError(f"{tool} was accepted, but the script expected it refused: {expected}")
    result = await replay.call(grant, tool, arguments)
    if isinstance(call, ListRepoDocsCall) and call.expect_paths is not None:
        got = result.model_dump()["paths"]
        if got != list(call.expect_paths):
            raise ReplayError(f"list_repo_docs returned {got}, expected {list(call.expect_paths)}")
    if isinstance(call, LinkMessageCall) and call.topic_key:
        replay.topics[TOPIC_KEY_PREFIX + call.topic_key] = result.model_dump()["topic_id"]


async def _compare(replay: _Replay, accepted: list[str], ignored: dict[str, str]) -> list[Check]:
    expected = replay.thread.expected
    checks = [
        _equal("ingest accepted", accepted, list(expected.accepted)),
        _equal("ingest ignored", ignored, dict(expected.ignored)),
    ]
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
        wanted = set(want.messages)
        if got != wanted:
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
    drafted: dict[
        tuple[str, str, str, int | None], list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]
    ] = {}
    for root in sorted({run.thread_ts for run in replay.thread.agent}):
        for proposal in await replay.store.thread_proposals(channel, root):
            draft = proposal.proposal
            topic = by_store_id.get(draft.topic_id or "", draft.topic_id or "")
            number = getattr(draft, "issue_number", None)
            key = (draft.kind, draft.repo.casefold(), topic, number)
            got_proposals[key] += 1
            if draft.kind == "create_issue":
                drafted.setdefault(key, []).append(
                    (draft.title, draft.body, tuple(draft.labels), tuple(draft.assignees))
                )
    want_proposals = Counter(p.key() for p in expected.proposals)
    checks.append(_counted("proposals", got_proposals, want_proposals))
    # How each new issue reads, where the fixture says (#249).
    form_problems: list[str] = []
    shaped = [p for p in expected.proposals if p.shaped]
    for shape in shaped:
        for drafted_issue in drafted.get(shape.key(), []):
            form_problems += shape.form_problems(*drafted_issue)
    if shaped:
        checks.append(
            Check(
                "issue form",
                not form_problems,
                "; ".join(form_problems) or f"{len(shaped)} as expected",
            )
        )
    checks.append(await _revision_check(replay))
    checks.append(_equal("asks", replay.poster.threads, list(expected.asks)))
    docs_read = []
    for run_id in replay.run_ids:
        docs_read += _doc_keys(await replay.store.tool_call_results(run_id, (READ_TOOL,)))
    checks.append(_equal("docs read", docs_read, list(expected.docs_read)))
    # ask: is a question, checked under questions, not a keyword a handler applies (#57).
    found = {
        m.ts: keyword
        for m in replay.accepted
        if (keyword := split_keyword(m.text)[0]) is not None and keyword != "ask"
    }
    checks.append(_equal("keywords", found, dict(expected.keywords)))
    checks += await _question_checks(replay)
    return checks


async def _revision_check(replay: _Replay) -> Check:
    """Each proposal was revised as often as expected, by whom, and in what (#304)."""
    channel = _channel(replay.thread)
    by_store_id = {v: k for k, v in replay.topics.items()}
    problems: list[str] = []
    expected = replay.thread.expected.proposals
    for root in sorted({run.thread_ts for run in replay.thread.agent}):
        for record in await replay.store.thread_proposals(channel, root):
            draft = record.proposal
            topic = by_store_id.get(draft.topic_id or "", draft.topic_id or "")
            key = (draft.kind, draft.repo.casefold(), topic, getattr(draft, "issue_number", None))
            want = next((p for p in expected if p.key() == key), None)
            if want is None:
                continue
            revisions = await replay.store.proposal_revisions(record.id)
            if len(revisions) != want.revisions:
                problems.append(f"{topic} revised {len(revisions)} times, not {want.revisions}")
                continue
            if revisions and want.revised_by and revisions[-1].added_by != tuple(want.revised_by):
                problems.append(f"{topic}'s last revision added {list(revisions[-1].added_by)}")
            if revisions and want.changed and revisions[-1].changed != tuple(want.changed):
                problems.append(f"{topic}'s last revision changed {list(revisions[-1].changed)}")
    total = sum(p.revisions for p in expected)
    return Check("revisions", not problems, "; ".join(problems) or f"{total} as expected")


async def _question_checks(replay: _Replay) -> list[Check]:
    """Questions JotFlow took, the answers it posted, and its private replies (#270)."""
    expected = replay.thread.expected
    channel = _channel(replay.thread)
    drafted = []
    for ts in expected.questions:
        if not is_ask(replay.thread.messages[ts].text or ""):
            continue  # a mention is drafted too (#329): it may join a topic
        link = await replay.store.get_message(channel, ts)
        if link is not None and link.topic_id is not None:
            drafted.append(ts)
    checks = [
        Check(
            "questions",
            not drafted,
            f"in a topic: {', '.join(drafted)}"
            if drafted
            else f"{len(expected.questions)} set aside",
        )
    ]
    posted = replay.poster.answers
    problems: list[str] = []
    got_threads = [thread for thread, _, _ in posted]
    want_threads = [a.thread for a in expected.answers]
    if got_threads != want_threads:
        problems.append(f"posted in {got_threads}, expected {want_threads}")
    else:
        for index, (want, (_, _, text)) in enumerate(zip(expected.answers, posted, strict=True)):
            problems += [
                f"answer {index} lacks {part!r}" for part in want.contains if part not in text
            ]
            problems += [f"answer {index} has {part!r}" for part in want.excludes if part in text]
            if replay.exact and want.text is not None and text != want.text:
                problems.append(f"answer {index} differs from the expected text")
            if replay.exact and want.removed is not None:
                got = replay.removed[index] if index < len(replay.removed) else None
                if got != want.removed:
                    problems.append(f"answer {index} removed {got}, expected {want.removed}")
    checks.append(
        Check("answers", not problems, "; ".join(problems) or f"{len(posted)} as expected")
    )
    whispers = replay.slack.whispers if replay.slack else []
    wanted = [(w.user, w.text) for w in expected.whispers]
    checks.append(
        Check(
            "private replies",
            whispers == wanted,
            f"{len(wanted)} as expected"
            if whispers == wanted
            else f"sent to {[u for u, _ in whispers]}, expected {[u for u, _ in wanted]}",
        )
    )
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
    attribution = FixtureAttribution(thread)
    bot = thread.workspace.bot
    reader = _FixtureReader(thread, messages)
    source = SlackWorkSource(reader, active, bot=BotIdentity(bot.user_id, bot.bot_id))
    repos = fixture_catalog(thread, clock)
    # Repo docs (#209): the fixture's files stand in for the drafting runner.
    docs = DocReads(active)
    fixture_docs = FixtureDocs(thread.docs)
    specs = {
        spec.name: spec
        for spec in (
            *read_tools(
                active, thread.policy, source, clock=clock, attribution=attribution, repos=repos
            ),
            *write_tools(
                active,
                thread.policy,
                poster,
                clock=clock,
                repos=repos,
                docs=docs,
                authors=attribution.authors,
            ),
            *doc_tools(active, thread.policy, lambda: fixture_docs, docs, clock=clock),
        )
    }
    state = _Replay(
        thread,
        active,
        clock,
        specs,
        poster,
        accepted=messages,
        attribution=attribution,
        slack=_FixtureSlack(thread),
        reader=reader,
    )
    _record_answers(state)
    registry = TokenRegistry(clock=clock.mono)
    lifecycle = RunLifecycle(active, registry, clock=clock)
    report = ReplayReport(thread.name, thread.description)
    await _seed(state)
    await _apply_keywords(state, accepted)
    return Prepared(state, lifecycle, registry, report, accepted, ignored)


def _record_answers(state: _Replay) -> None:
    """Note what each answer's rewrite removed, in process or over MCP alike (#270)."""
    spec = state.specs.get("answer")
    if spec is None:  # pragma: no cover - the write tools always include it
        return
    real = spec.handler

    async def handler(grant: Grant, arguments: Any) -> BaseModel:
        result = await real(grant, arguments)
        state.removed.append(int(result.model_dump()["removed"]))
        return result

    state.specs["answer"] = replace(spec, handler=handler)


async def finish(prepared: Prepared, *, exact: bool = True) -> ReplayReport:
    """Compare the store with the fixture's expected outcome, and add the invariants.

    ``exact`` is for the scripted agent: an answer's exact words are checked too.
    """
    state = prepared.state
    state.exact = exact
    prepared.report.checks += await _compare(state, prepared.accepted, prepared.ignored)
    prepared.report.checks += await invariants(state)
    return prepared.report


async def replay(thread: SavedThread, *, store: Store | None = None) -> ReplayReport:
    """Run ``thread`` through the pipeline and compare it with its expected outcome."""
    prepared = await prepare(thread, store=store)
    state, report = prepared.state, prepared.report
    try:
        await run_script(state, prepared.lifecycle, prepared.registry, state.accepted)
    except ReplayError as exc:
        report.checks.append(Check("script", False, str(exc)))
    else:
        report.checks.append(Check("script", True, f"{len(thread.agent)} run(s) completed"))
    # Compare even after a refused call, so the report shows what differs.
    return await finish(prepared)


# Anything that could link or ping, found on its own terms rather than with the
# answer rewrite's patterns, so a gap in the rewrite can't pass its own check (#270).
_LINK_OR_PING = re.compile(r"(?i)https?://|www\.|<@|<!|<#|@[A-Za-z0-9-]+|\b[a-z0-9-]+\.[a-z]{2,}/")


async def _answer_checks(replay: _Replay) -> list[Check]:
    """What every posted answer keeps to, whatever the fixture expects (#57, #270)."""
    from jotflow.whiteboard.write_tools import ANSWER_PREFIX

    posted = replay.poster.answers
    asked_in = {run.thread_ts for run in replay.thread.agent if run.kind == "question"}
    unlabelled = [ts for _, ts, text in posted if not text.startswith(ANSWER_PREFIX + " ")]
    leaks = [ts for _, ts, text in posted if _LINK_OR_PING.search(text)]
    elsewhere = [thread for thread, _, _ in posted if thread not in asked_in]
    # As live, JotFlow's own post goes back through ingest: it must never ask a question.
    bot = replay.thread.workspace.bot
    events = EventFilter(replay.thread.policy.channels, BotIdentity(bot.user_id, bot.bot_id))
    questions = _questions(replay)
    channel = _channel(replay.thread)
    looped: list[str] = []
    for thread, ts, text in posted:
        event = {
            "type": "message",
            "channel": channel,
            "ts": ts,
            "thread_ts": thread,
            "user": bot.user_id,
            "bot_id": bot.bot_id,
            "text": text,
        }
        if not isinstance(events.classify(event), Accepted):
            continue
        if (await questions.on_message(event)).outcome == "queued":
            looped.append(ts)
    return [
        Check(
            "answers are labelled",
            not unlabelled,
            f"unlabelled: {', '.join(unlabelled)}" if unlabelled else f"{len(posted)} labelled",
        ),
        Check(
            "no link or ping in an answer",
            not leaks,
            f"in answer {', '.join(leaks)}" if leaks else "none",
        ),
        Check(
            "answers stay in their thread",
            not elsewhere,
            f"posted in {', '.join(elsewhere)}" if elsewhere else "all in place",
        ),
        Check(
            "no question from JotFlow's own post",
            not looped,
            f"queued from {', '.join(looped)}" if looped else "none",
        ),
    ]


async def invariants(replay: _Replay) -> list[Check]:
    """Rules every run must keep, whatever the fixture expects (#23)."""
    channel = _channel(replay.thread)
    logins = {u.github.casefold() for u in replay.thread.users.values() if u.github}
    thread_text = "\n".join(m.text or "" for m in replay.accepted)
    from_chatter: list[str] = []
    unlinked: list[str] = []
    invented: list[str] = []
    commands: list[str] = []
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
            written = (
                draft.body
                if draft.kind == "create_issue"
                else "\n".join(t for t in (draft.changes.body, draft.changes.comment) if t)
            )
            invented += invented_numbers(written, thread_text)
            invented += invented_links(written, thread_text)
            commands += invented_commands(written, thread_text)
    return [
        *await _answer_checks(replay),
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
        # No fabricated facts (#249): an acceptance number or a link nobody gave.
        Check(
            "no invented numbers or links",
            not invented,
            f"nobody gave: {', '.join(invented)}" if invented else "nothing invented",
        ),
        # A plan a coding agent may act on carries no command nobody wrote (#249).
        Check(
            "no commands nobody gave",
            not commands,
            f"commands: {' / '.join(commands)}" if commands else "none",
        ),
    ]
