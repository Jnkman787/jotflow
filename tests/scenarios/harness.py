"""One thread, several features, step by step (#380).

Each 0.5.0 feature had tests of its own, but none ran two of them in one thread,
and eleven bugs hid where they met (#373 to #376, #378). A scenario here plays one
thread through JotFlow's real pieces, in the order the service runs them:

- ingest's filter, keywords and the debouncer, for each message and reaction;
- a drafting run through the run lifecycle, its token, and the real read and write
  tools, with the scripted calls an agent would make;
- the card's buttons, through the real approval gate, Add me and card edits;
- the real card sync, so every step draws the cards a person would see.

Only Slack, GitHub and the agent are fakes. A scenario asserts proposal states,
card text and credits after every step, not just at the end. Each runs on both
stores.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.board_catalog import BoardCatalog
from jotflow.change_requests import CHANGE_REQUEST_EVENT, change_request_by
from jotflow.config import Settings
from jotflow.models import (
    FoundBoard,
    MessageRef,
    ProposalRecord,
    ProposalState,
    RunKind,
    RunStatus,
)
from jotflow.name_match import Names
from jotflow.orchestrator.add_me import AddMe, Assignable, Membership
from jotflow.orchestrator.approval import (
    CALLBACK_CHANGES,
    CHANGES_BLOCK,
    CHANGES_INPUT,
    ApprovalHandler,
)
from jotflow.orchestrator.card_edits import CardEdits
from jotflow.orchestrator.card_sync import CardSync
from jotflow.orchestrator.cards import (
    ACTION_ADD_ME,
    ACTION_APPROVE,
    ACTION_BOARDS,
    BOARDS_BLOCK,
    Card,
    CardPublisher,
)
from jotflow.orchestrator.debounce import Debouncer
from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter
from jotflow.orchestrator.keyword_handler import KeywordHandler, Person
from jotflow.orchestrator.packet import SlackWorkSource
from jotflow.orchestrator.questions import QuestionHandler
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.orchestrator.service import Orchestrator
from jotflow.orchestrator.slack_files import TooLarge
from jotflow.orchestrator.summary import WeeklySummaries, summary_metadata
from jotflow.orchestrator.summary_replies import SummaryReplies
from jotflow.policy import Policy, parse_policy
from jotflow.store import Store, content_hash
from jotflow.whiteboard.read_tools import Work, read_tools
from jotflow.whiteboard.tokens import Grant, TokenRegistry
from jotflow.whiteboard.tools import ToolError, ToolSpec
from jotflow.whiteboard.write_tools import write_tools

CH = "C0EXAMPLE1"
TEAM = "T0EXAMPLE"
BOT = BotIdentity("U0JOTFLOW", "B0JOTFLOW")
START = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
STEP = timedelta(seconds=20)  # between two things people do

ALICE, BOB, CAROL, DAVE = "U0ALICE", "U0BOB", "U0CAROL", "U0DAVE"
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol", DAVE: "Dave"}
# Each person's linked GitHub login and account id. All four proved theirs.
LOGINS = {ALICE: ("acme-alice", 1101), BOB: ("acme-bob", 1102), CAROL: ("acme-carol", 1103)}

POLICY = f"""
[channels.{CH}]
debounce_seconds = 60

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app: dashboard, exports and sign-in"
labels = ["bug", "enhancement"]

[[channels.{CH}.repos]]
repo = "acme-co/sensors"
nickname = "sensors"
description = "Sensor firmware and its timing"
labels = ["bug", "enhancement"]
"""


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def mono(self) -> float:
        return (self.now - START).total_seconds()


class Slack:
    """The workspace, as JotFlow's Slack calls see it: messages, people, posts.

    One fake answers every Slack protocol the pieces use, so a card JotFlow posts
    or a change request it writes is in the thread the next run reads.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.messages: dict[str, dict[str, Any]] = {}
        self.files: dict[str, tuple[dict[str, Any], str]] = {}  # id -> (Slack's file, its text)
        self.cards: dict[str, Card] = {}  # by the card's ts, as last drawn
        self.whispers: list[tuple[str, str]] = []  # (user, text)
        self.reactions: list[tuple[str, str]] = []  # JotFlow's own: (ts, name)
        self._own = 0

    def ts(self) -> str:
        return f"{int(self._clock.now.timestamp())}.000100"

    def own_ts(self) -> str:
        self._own += 1
        return f"{int(self._clock.now.timestamp())}.{900000 + self._own:06d}"

    def add(self, message: dict[str, Any]) -> None:
        assert message["ts"] not in self.messages, f"two messages at {message['ts']}"
        self.messages[message["ts"]] = message

    def post_own(self, thread_ts: str, text: str, **extra: Any) -> str:
        ts = self.own_ts()
        self.add(
            {
                "type": "message",
                "channel": CH,
                "ts": ts,
                "thread_ts": thread_ts,
                "user": BOT.user_id,
                "bot_id": BOT.bot_id,
                "text": text,
                **extra,
            }
        )
        return ts

    # The packet's reads (SlackReader)

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        # Asked for a reply, Slack answers with the reply's whole thread.
        root = self.messages[ts].get("thread_ts", ts) if ts in self.messages else ts
        thread = [
            m
            for m in self.messages.values()
            if m["channel"] == channel and m.get("thread_ts", m["ts"]) == root
        ]
        thread.sort(key=lambda m: (m["ts"] != root, float(m["ts"])))
        return {"ok": True, "messages": thread[:limit], "has_more": False}

    async def history(
        self, channel: str, *, oldest: str, latest: str, limit: int
    ) -> Mapping[str, Any]:
        posts = [
            m
            for m in self.messages.values()
            if m["channel"] == channel
            and m.get("thread_ts", m["ts"]) == m["ts"]
            and float(oldest) < float(m["ts"]) < float(latest)
        ]
        posts.sort(key=lambda m: float(m["ts"]), reverse=True)
        return {"ok": True, "messages": posts[:limit]}

    async def user(self, user_id: str) -> Mapping[str, Any]:
        name = NAMES.get(user_id, user_id)
        return {"ok": True, "user": {"id": user_id, "profile": {"display_name": name}}}

    # Files (#333): files.info and the download

    def add_file(self, file_id: str, name: str, text: str) -> dict[str, Any]:
        described = {
            "id": file_id,
            "name": name,
            "filetype": "text",
            "mimetype": "text/plain",
            "size": len(text.encode()),
            "url_private": f"https://files.slack.com/files-pri/{TEAM}-{file_id}/{file_id}",
            "channels": [CH],
        }
        self.files[file_id] = (described, text)
        return described

    async def info(self, file_id: str) -> Mapping[str, Any]:
        if file_id not in self.files:
            raise LookupError("file_not_found")
        return self.files[file_id][0]

    async def download(self, url: str, limit: int) -> bytes:
        file_id = url.rsplit("/", 1)[-1]
        if file_id not in self.files:
            raise LookupError("file_not_found")
        data = self.files[file_id][1].encode()
        if len(data) > limit:
            raise TooLarge
        return data

    # Attribution, for the write tools and the card

    async def authors(self, refs: Sequence[MessageRef]) -> set[str]:
        found: set[str] = set()
        for ref in refs:
            message = self.messages.get(ref.ts)
            if message is None:
                raise LookupError(ref.ts)
            requester = change_request_by(message, BOT.user_id)
            if requester is not None:
                found.add(requester)
            elif message.get("user") and message["user"] != BOT.user_id:
                found.add(message["user"])
        return found

    async def mentions(self, ref: MessageRef) -> set[str]:
        text = self.messages[ref.ts].get("text") or ""
        return set(re.findall(r"<@([UW][A-Z0-9]{2,})>", text))

    async def text(self, ref: MessageRef) -> str:
        message = self.messages.get(ref.ts)
        if message is None:
            raise LookupError(ref.ts)
        return str(message.get("text") or "")

    async def names(self, user_id: str) -> Names:
        return Names(display=NAMES.get(user_id))

    async def name(self, user_id: str) -> str | None:
        return NAMES.get(user_id)

    # The gate, Add me, keywords and card edits

    async def own_team(self) -> str | None:
        return TEAM

    async def person(self, user_id: str) -> Person:
        return Person(user_id)

    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None:
        self.whispers.append((user_id, text))

    async def open_modal(self, trigger_id: str, view: Mapping[str, Any]) -> None:
        raise AssertionError("a scenario's people are linked: no dialog opens")

    async def update_modal(self, view_id: str, view: Mapping[str, Any]) -> None:
        raise AssertionError("a scenario's people are linked: no dialog opens")

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        self.post_own(thread_ts, text)

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self.reactions.append((ts, name))

    async def change_request(
        self, channel_id: str, thread_ts: str, text: str, by: str, **target: Any
    ) -> None:
        """As BoltApprovalSlack posts it: JotFlow's message, carrying who asked."""
        from jotflow.change_requests import change_request_metadata

        self.post_own(thread_ts, text, metadata=change_request_metadata(by, **target))

    # Cards (CardPoster)

    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        ts = self.post_own(thread_ts, card.text)
        self.cards[ts] = card
        return ts

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        self.cards[ts] = card


class Checks:
    """GitHub's word on each login, for Add me: every linked login can be assigned."""

    async def assignable(self, owner: str, name: str, login: str) -> Assignable:
        for found, account_id in LOGINS.values():
            if found == login:
                return Assignable(account="user", id=account_id, login=login, assignable=True)
        return Assignable(account="missing")

    async def membership(self, org: str, login: str) -> Membership:
        return Membership(member="yes")


class SummaryPosts:
    """Where the weekly summary goes: a top-level post from JotFlow (#308)."""

    def __init__(self, slack: Slack) -> None:
        self._slack = slack
        self.posts: dict[str, list[dict[str, Any]]] = {}  # ts -> blocks

    async def post(
        self, channel_id: str, text: str, blocks: list[dict[str, Any]], slot: datetime
    ) -> str:
        ts = self._slack.own_ts()
        self._slack.add(
            {
                "type": "message",
                "channel": channel_id,
                "ts": ts,
                "user": BOT.user_id,
                "bot_id": BOT.bot_id,
                "text": text,
                "metadata": summary_metadata(slot),
            }
        )
        self.posts[ts] = blocks
        return ts

    async def find(self, channel_id: str, slot: datetime) -> str | None:
        return None

    async def update(
        self, channel_id: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> None:
        self.posts[ts] = blocks

    async def permalink(self, channel_id: str, ts: str) -> str | None:
        return f"https://acme-co.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


class Replies:
    """The Slack calls a reply in JotFlow's own thread makes (#318)."""

    def __init__(self, slack: Slack) -> None:
        self._slack = slack

    async def person(self, user_id: str) -> Person:
        return Person(user_id)

    async def name(self, user_id: str) -> str | None:
        return NAMES.get(user_id)

    async def workspace_url(self) -> str | None:
        return "https://acme-co.slack.com/"

    async def ephemeral(self, channel_id: str, user_id: str, thread_ts: str, text: str) -> None:
        self._slack.whispers.append((user_id, text))

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self._slack.reactions.append((ts, name))

    async def note(self, channel_id: str, thread_ts: str, text: str) -> None:
        self._slack.post_own(thread_ts, text)


class Questions:
    """The Slack calls a question makes (#57)."""

    def __init__(self, slack: Slack) -> None:
        self._slack = slack

    async def person(self, user_id: str) -> Person:
        return Person(user_id)

    async def own_team(self) -> str | None:
        return TEAM

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        self._slack.whispers.append((user_id, text))

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self._slack.reactions.append((ts, name))


def _wire(service: Orchestrator, **parts: Any) -> None:
    """Give the service its parts, as its start() does. Each must already exist on
    it, so a rename fails here rather than leaving a part unused."""
    for name, part in parts.items():
        assert hasattr(service, name), f"the service has no {name}"
        setattr(service, name, part)


def card_text(card: Card) -> str:
    """Every piece of text on the card, in order, one line each."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            text = node.get("text")
            if isinstance(text, str):
                found.append(text)
            for key, value in node.items():
                if key != "text" or not isinstance(value, str):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card.blocks)
    return "\n".join(found)


@dataclass
class Run:
    """One drafting run under way: its packet, and the agent's calls."""

    scenario: Scenario
    grant: Grant
    work: Work

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        return await self.scenario.call(self.grant, tool, arguments)

    async def refused(self, tool: str, arguments: dict[str, Any]) -> str:
        """The call is refused. Returns why."""
        try:
            await self.call(tool, arguments)
        except ToolError as refused:
            return refused.reason
        raise AssertionError(f"{tool} was accepted")

    async def end(self) -> None:
        """The run ends, and the card sync draws what it proposed."""
        await self.scenario.lifecycle.finish(self.grant.run_id, RunStatus.SUCCEEDED)
        await self.scenario.sync.sync()

    def topic(self, title: str) -> Any:
        """The packet's topic with ``title``."""
        assert self.work.conversation is not None
        for topic in self.work.conversation.topics.topics:
            if topic.title.text == title:
                return topic
        raise AssertionError(f"no topic {title!r} in the packet")

    def message(self, ts: str) -> Any:
        assert self.work.conversation is not None
        for message in self.work.conversation.thread.messages:
            if message.ts == ts:
                return message
        raise AssertionError(f"message {ts} isn't in the packet")


class Scenario:
    """One channel's live pieces over one store, driven a step at a time."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        policy: str = POLICY,
        *,
        found: Sequence[FoundBoard] = (),
    ) -> None:
        """``found`` is what the channel's first board scope lists (#309), if any."""
        self.store = store
        self.clock = clock
        self.policy: Policy = parse_policy(policy)
        self.boards = BoardCatalog(self.policy, clock=self.clock)
        if found:
            self.boards.load(CH, 0, found)
        self.slack = Slack(self.clock)
        self.ingest = EventFilter([CH], BOT)
        self.keywords = KeywordHandler(store, self.policy, self.slack)
        self.debouncer = Debouncer(store, self.policy, clock=self.clock)
        self.registry = TokenRegistry(clock=self.clock.mono)
        self._summaries: set[str] = set()
        self.lifecycle = RunLifecycle(store, self.registry, clock=self.clock)
        source = SlackWorkSource(
            self.slack,
            store,
            bot=BOT,
            files=self.slack,
            attachments_for=self.policy.attachments_for,
        )
        ids = iter(f"{n:016x}" for n in range(1, 10_000))
        self.specs: dict[str, ToolSpec] = {
            spec.name: spec
            for spec in (
                *read_tools(
                    store,
                    self.policy,
                    source,
                    clock=self.clock,
                    attribution=self.slack,
                    files=self.slack,
                    boards=self.boards,
                ),
                *write_tools(
                    store,
                    self.policy,
                    self.slack_poster(),
                    clock=self.clock,
                    new_id=lambda: next(ids),
                    authors=self.slack.authors,
                    mentions=self.slack.mentions,
                    texts=self.slack.text,
                    names=self.slack.names,
                    files=self.slack,
                    boards=self.boards,
                ),
            )
        }
        self.sync = CardSync(
            store,
            self.policy,
            CardPublisher(store, self.slack),
            self.slack,
            clock=self.clock,
            arm_thread=self.arm,
            boards=self.boards,
        )
        self.gate = ApprovalHandler(
            store, self.policy, self.slack, refresh=self._refresh, arm_thread=self.arm
        )
        self_add_ids = iter(f"{n:016x}" for n in range(0xA000, 0xB000))
        self.add_me_handler = AddMe(
            store,
            self.policy,
            self.gate,
            self.slack,
            lambda: Checks(),
            refresh=self._refresh,
            clock=self.clock,
            monotonic=self.clock.mono,
            new_id=lambda: next(self_add_ids),
        )
        self.edits = CardEdits(
            store,
            self.policy,
            self.gate.guarded,
            self.slack,
            refresh=self._refresh,
            boards=self.boards,
        )
        self.summary_posts = SummaryPosts(self.slack)
        self.summaries = WeeklySummaries(
            store, self.policy, self.summary_posts, self.slack, clock=self.clock
        )
        self.questions = QuestionHandler(
            store,
            self.policy,
            Questions(self.slack),
            BOT.user_id,
            clock=self.clock,
            monotonic=self.clock.mono,
        )
        self.replies = SummaryReplies(
            store,
            self.policy,
            Replies(self.slack),
            bot_user_id=BOT.user_id,
            clock=self.clock,
            monotonic=self.clock.mono,
        )
        # The service's own event handling, so each message and reaction goes through
        # its guards in its order: the summary's thread, questions, keywords, the
        # card's reaction, then the debouncer.
        self.service = Orchestrator(
            Settings(_env_file=None, slack_bot_token="xoxb-test", slack_app_token="xapp-test"),  # type: ignore[call-arg]
            self.policy,
            AsyncWebClient(token="xoxb-test"),
            BOT,
            make_server=lambda *_: None,  # type: ignore[arg-type,return-value]
        )
        _wire(
            self.service,
            _store=store,
            _keywords=self.keywords,
            _questions=self.questions,
            summary_replies=self.replies,
            approval=self.gate,
            _debouncer=self.debouncer,
            _help=None,
        )

    def slack_poster(self) -> Any:
        slack = self.slack

        class Poster:
            async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
                return slack.post_own(thread_ts, text)

            async def post_plain(self, channel_id: str, thread_ts: str, text: str) -> str:
                return slack.post_own(thread_ts, text)

        return Poster()

    async def setup(self) -> Scenario:
        for user, (login, account_id) in LOGINS.items():
            await self.store.finish_verification(user, github_login=login, github_id=account_id)
        return self

    async def _refresh(self, proposal_id: str) -> None:
        await self.sync.refresh(proposal_id)

    async def arm(self, channel_id: str, thread_ts: str) -> None:
        await self.debouncer.arm(channel_id, thread_ts)

    def later(self, step: timedelta = STEP) -> None:
        self.clock.now += step

    # People in Slack

    async def say(
        self,
        user: str,
        text: str,
        *,
        thread: str | None = None,
        files: Sequence[tuple[str, str, str]] = (),
    ) -> str:
        """Post a message as ``user``, through ingest, keywords and the debouncer.

        Each of ``files`` is (id, name, text), shared with the message.
        """
        self.later()
        event: dict[str, Any] = {
            "type": "message",
            "channel": CH,
            "ts": self.slack.ts(),
            "user": user,
            "text": text,
        }
        if thread is not None:
            event["thread_ts"] = thread
            event["parent_user_id"] = self.slack.messages[thread].get("user")
        if files:
            event["subtype"] = "file_share"
            event["files"] = [self.slack.add_file(*file) for file in files]
        self.slack.add(event)
        decision = self.ingest.classify(event)
        assert isinstance(decision, Accepted), decision
        await self.service.note(decision, event)
        return str(event["ts"])

    async def react(self, user: str, name: str, ts: str, *, removed: bool = False) -> None:
        """React to message ``ts`` as ``user``, or take the reaction back."""
        self.later()
        message = self.slack.messages[ts]
        event = {
            "type": "reaction_removed" if removed else "reaction_added",
            "user": user,
            "reaction": name,
            "item_user": message.get("user"),
            "item": {"type": "message", "channel": CH, "ts": ts},
        }
        decision = self.ingest.classify(event)
        assert isinstance(decision, Accepted), decision
        await self.service.note(decision, event)
        await self.sync.sync()

    async def post_summary(self) -> str:
        """Post the channel's weekly summary, due now. Returns its ts."""
        assert await self.summaries.tick() == [CH]
        (ts,) = [
            t
            for t, m in self.slack.messages.items()
            if m.get("metadata", {}).get("event_type") == summary_metadata(START)["event_type"]
            and t not in self._summaries
        ]
        self._summaries.add(ts)
        return ts

    async def quiet(self) -> list[str]:
        """Let every waiting thread go quiet. Returns the threads queued for drafting."""
        self.later(timedelta(seconds=61))
        return [t.thread_ts for t in await self.debouncer.flush()]

    # The agent

    async def run(self, thread: str) -> Run:
        """Start the thread's queued drafting run and read its packet, as an agent does."""
        started = await self.lifecycle.start_next(max_running=4, kinds={RunKind.DRAFTING})
        assert started is not None, f"no drafting run was queued for {thread}"
        assert started.run.thread_ts == thread, started.run.thread_ts
        grant = self.registry.check(started.token)
        assert grant is not None
        work = await self.call(grant, "get_work", {})
        assert isinstance(work, Work)
        return Run(self, grant, work)

    async def call(self, grant: Grant, tool: str, arguments: dict[str, Any]) -> BaseModel:
        spec = self.specs[tool]
        return await spec.handler(grant, spec.input_model.model_validate(arguments))

    # The card's buttons

    async def request_changes(self, proposal_id: str, user: str, text: str) -> None:
        self.later()
        body = {
            "type": "view_submission",
            "user": {"id": user, "team_id": TEAM},
            "team": {"id": TEAM},
            "view": {
                "callback_id": CALLBACK_CHANGES,
                "private_metadata": proposal_id,
                "state": {"values": {CHANGES_BLOCK: {CHANGES_INPUT: {"value": text}}}},
            },
        }
        refused = await self.gate.on_changes_submitted(body)
        assert refused is None, refused

    async def add_me(self, proposal_id: str, user: str) -> str:
        self.later()
        record = await self.proposal(proposal_id)
        assert record.card_ts is not None
        outcome = await self.add_me_handler.on_card(
            self._click(ACTION_ADD_ME, f"{proposal_id}:me", user, record.card_ts)
        )
        await self.sync.sync()
        return outcome

    async def approve(self, proposal_id: str, user: str) -> str:
        self.later()
        record = await self.proposal(proposal_id)
        assert record.card_ts is not None
        value = f"{proposal_id}:{content_hash(record.proposal)}"
        outcome = await self.gate.on_approve(
            self._click(ACTION_APPROVE, value, user, record.card_ts)
        )
        await self.sync.sync()
        return outcome

    async def pick_boards(self, proposal_id: str, user: str, *nicknames: str) -> str:
        """Choose ``nicknames`` on the card's board menu, as ``user`` (#309)."""
        self.later()
        record = await self.proposal(proposal_id)
        assert record.card_ts is not None
        body = self._click(ACTION_BOARDS, "", user, record.card_ts)
        body["actions"] = [
            {
                "action_id": ACTION_BOARDS,
                "block_id": f"{BOARDS_BLOCK}:{proposal_id}",
                "type": "multi_static_select",
                "selected_options": [{"value": n} for n in nicknames],
            }
        ]
        outcome = await self.edits.on_boards(body)
        await self.sync.sync()
        return outcome

    def _click(self, action: str, value: str, user: str, card_ts: str) -> dict[str, Any]:
        return {
            "type": "block_actions",
            "user": {"id": user, "team_id": TEAM},
            "team": {"id": TEAM},
            "channel": {"id": CH},
            "container": {"type": "message", "message_ts": card_ts, "channel_id": CH},
            "trigger_id": "1.2.trigger",
            "actions": [{"action_id": action, "type": "button", "value": value}],
        }

    # What a person would see

    async def proposal(self, proposal_id: str) -> ProposalRecord:
        record = await self.store.get_proposal(proposal_id)
        assert record is not None, proposal_id
        return record

    async def states(self, thread: str) -> dict[str, ProposalState]:
        return {r.id: r.state for r in await self.store.thread_proposals(CH, thread)}

    async def card(self, proposal_id: str) -> str:
        """The card's text as last drawn in Slack."""
        record = await self.proposal(proposal_id)
        assert record.card_ts is not None, f"{proposal_id} has no card"
        return card_text(self.slack.cards[record.card_ts])

    def change_requests(self, thread: str) -> list[dict[str, Any]]:
        """JotFlow's posts of change requests in ``thread``, oldest first."""
        return [
            m
            for ts, m in sorted(self.slack.messages.items(), key=lambda kv: float(kv[0]))
            if m.get("thread_ts") == thread
            and (m.get("metadata") or {}).get("event_type") == CHANGE_REQUEST_EVENT
        ]
