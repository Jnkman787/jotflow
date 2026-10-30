"""Random sequences of store and tool operations, on both stores, checked after every step (#381).

Several 0.5.0 bugs were states no handwritten test reached: an empty topic still
holding a pending draft, an Add me request rewritten by a revision, a sibling draft
superseded. Hypothesis drives one channel and thread through posts, splits,
proposals, revisions, change requests, approvals, skip:, re:, chatter marks, Add
me on cards and filed issues, rebuild and summary-thread replies, on MemoryStore
and SqliteStore side by side, and checks the invariants below after each step.

The drafting agent is played by the machine, from what get_work serves: it sees a
pending draft's assignees, and the reasons JotFlow shows it, and keeps the reasons
it gave itself, never a person's card add, which it can't give.

Profiles: ``ci`` (the default, bounded so ``make check`` stays fast) and
``nightly``, set with ``JOTFLOW_HYPOTHESIS_PROFILE=nightly``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from jotflow.models import (
    ExecutionResult,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    RunKind,
    RunStatus,
    UpdateProposal,
)
from jotflow.name_match import Names
from jotflow.orchestrator.add_me import AddMe
from jotflow.orchestrator.approval import ApprovalHandler
from jotflow.orchestrator.cards import ACTION_ADD_ME, ACTION_ADD_ME_FILED
from jotflow.orchestrator.ingest import BotIdentity
from jotflow.orchestrator.slack_files import SERVED_FILE
from jotflow.policy import Policy
from jotflow.rebuild import ChannelPlan, ThreadPlan
from jotflow.rebuild import apply as rebuild_apply
from jotflow.store import MemoryStore, SqliteStore, Store, content_hash
from jotflow.store.base import SELF_ADD_RUN, ToolCallRecord
from jotflow.whiteboard.read_tools import (
    NoArguments,
    ThreadMessage,
    TopicIndex,
    UntrustedText,
    Work,
    WorkThread,
    read_tools,
)
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError, checked_registry
from jotflow.whiteboard.write_tools import (
    LinkMessageArgs,
    ProposeIssueArgs,
    ReviseProposalArgs,
    WriteTools,
)
from tests.test_add_me import AddMeSlack, Checks
from tests.test_attachments import Files

settings.register_profile(
    "ci",
    max_examples=12,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
settings.register_profile(
    "nightly",
    max_examples=400,
    stateful_step_count=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
settings.register_profile(
    "smoke", max_examples=1, stateful_step_count=8, deadline=None, database=None
)
settings.load_profile(os.environ.get("JOTFLOW_HYPOTHESIS_PROFILE", "ci"))

# A comma-separated list of invariant names to check, for chasing one bug past others
# that would stop a run first. Unset: all of them.
ONLY = {n for n in os.environ.get("JOTFLOW_STATEFUL_ONLY", "").split(",") if n}


def checking(name: str) -> bool:
    return not ONLY or name in ONLY


CH = "C0EXAMPLE1"
ROOT = "1792310400.000100"
SUMMARY = "1792320000.000100"  # the weekly summary's thread
TEAM = "T0OURS"
BOT = BotIdentity(user_id="U0JOTBOT", bot_id="B0JOTBOT")
ALICE, BOB, CAROL = "U0ALICE", "U0BOB", "U0CAROL"
LOGINS = {ALICE: ("octo-alex", 583231), BOB: ("octo-chris", 777001)}
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol"}
START = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
FILE = "F0DOC1"  # on the root message


@dataclass(frozen=True)
class Message:
    ts: str
    author: str
    text: str
    file: bool = False


# The thread's possible messages, posted in any order. Bob is named in one.
MESSAGES = [
    Message(ROOT, CAROL, "Notes: mixer runs twice; sensor readings are late.", file=True),
    Message("1792310460.000100", ALICE, "I can take the mixer part."),
    Message("1792310520.000100", CAROL, "Bob should own the sensor work."),
    Message("1792310580.000100", BOB, "Readings every second would do."),
    Message("1792310640.000100", ALICE, "Also show one cycle in the dashboard."),
]
BY_TS = {m.ts: m for m in MESSAGES}

POLICY = Policy.model_validate(
    {
        "channels": {
            CH: {
                "repos": [
                    {
                        "repo": "acme-co/web",
                        "nickname": "web",
                        "description": "Web",
                        "labels": ["bug", "enhancement"],
                    }
                ],
                "attachments": "text",
            }
        },
        # With an approver, an Add me request on a filed issue waits for approval, so a
        # revision can reach it, as #378 found.
        "approval": {"approvers": [CAROL]},
    }
)


class Source:
    """get_work's thread: the posted messages, as Slack would give them."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def thread(self, channel_id: str, thread_ts: str) -> WorkThread:
        messages = []
        for ts in sorted(self.world.posted):
            known = BY_TS.get(ts)
            author = known.author if known else self.world.extra_authors[ts]
            link = await self.world.store.get_message(channel_id, ts)
            messages.append(
                ThreadMessage(
                    ts=ts,
                    user_id=author,
                    author=UntrustedText(text=NAMES[author]),
                    text=UntrustedText(text=known.text if known else "A change, please."),
                    topic_id=link.topic_id if link else None,
                    also_topics=list(link.also_topics) if link else [],
                )
            )
        return WorkThread(channel_id=channel_id, thread_ts=thread_ts, messages=messages)

    async def related(self, thread: WorkThread, topics: TopicIndex) -> list[Any]:
        return []


@dataclass
class World:
    """One store and the tools over it. The machine runs two, one per store."""

    store: Store
    posted: set[str] = field(default_factory=set)
    extra_authors: dict[str, str] = field(default_factory=dict)
    grant: Grant | None = None
    tools: WriteTools | None = None
    reads: dict[str, Any] = field(default_factory=dict)
    add_me: AddMe | None = None
    ids: Any = None
    # What the machine, as the agent, gave each proposal: login -> (why, source_ts).
    agent_reasons: dict[str, dict[str, tuple[str, str | None]]] = field(default_factory=dict)
    # An Add me record as its requester left it, and card adds a person made.
    self_adds: dict[str, str] = field(default_factory=dict)
    card_adds: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        counter = iter(range(1, 10_000))
        self.ids = lambda: f"{next(counter):016x}"
        self.tools = WriteTools(
            self.store,
            POLICY,
            _Poster(),
            clock=lambda: START,
            new_id=self.ids,
            files=Files(),
            authors=self._authors,
            mentions=_no_mentions,
            texts=self._text,
            names=_names,
        )
        self.reads = checked_registry(
            read_tools(self.store, POLICY, Source(self), clock=lambda: START)
        )
        gate = ApprovalHandler(self.store, POLICY, _Slack())
        add_ids = iter(range(1, 10_000))
        self.add_me = AddMe(
            self.store,
            POLICY,
            gate,
            _Slack(),
            lambda: Checks(),
            clock=lambda: START,
            new_id=lambda: f"{0xA000 + next(add_ids):016x}",
        )

    async def _authors(self, refs: Any) -> set[str]:
        found = set()
        for ref in refs:
            known = BY_TS.get(ref.ts)
            found.add(known.author if known else self.extra_authors.get(ref.ts, CAROL))
        return found

    async def _text(self, ref: MessageRef) -> str:
        known = BY_TS.get(ref.ts)
        return known.text if known else "A change, please."


class _Poster:
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        return "1792399999.000100"

    async def post_plain(self, channel_id: str, thread_ts: str, text: str) -> str:
        return "1792399999.000100"


class _Slack(AddMeSlack):
    async def own_team(self) -> str | None:
        return TEAM


async def _no_mentions(ref: MessageRef) -> set[str]:
    return set()


async def _names(user: str) -> Names:
    return Names(display=NAMES.get(user))


def _click(action: str, value: str, user: str, ts: str, thread: str | None) -> dict[str, Any]:
    container: dict[str, Any] = {"type": "message", "message_ts": ts, "channel_id": CH}
    if thread is not None:
        container["thread_ts"] = thread
    return {
        "type": "block_actions",
        "user": {"id": user, "team_id": TEAM},
        "team": {"id": TEAM},
        "channel": {"id": CH},
        "container": container,
        "trigger_id": "1.2.trigger",
        "actions": [{"action_id": action, "type": "button", "value": value}],
    }


def _snapshot_self_add(record: ProposalRecord) -> str:
    return json.dumps(
        {"draft": record.proposal.model_dump(mode="json"), "who": record.participants},
        sort_keys=True,
    )


class StoreMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.dir = Path(tempfile.mkdtemp(prefix="jotflow-stateful-"))
        sqlite = self.run(SqliteStore.open(self.dir / "jotflow.db", clock=lambda: START))
        self.worlds = [World(MemoryStore(clock=lambda: START)), World(sqlite)]
        self.titles = 0
        self.created: list[str] = []  # topic titles that exist, in both stores
        self.change_requests = 0
        self.summary_replies = 0
        try:
            for world in self.worlds:
                for user, (login, account) in LOGINS.items():
                    self.run(world.store.admin_link(user, login, account))
            self.start_run()
        except BaseException:
            self.teardown()  # an open SQLite thread would keep the process alive
            raise

    def run(self, coroutine: Any) -> Any:
        return self.loop.run_until_complete(coroutine)

    def teardown(self) -> None:
        for world in self.worlds:
            self.run(world.store.close())
        self.loop.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def both(self, act: Any) -> list[Any]:
        """Run ``act(world)`` on each world. Both refuse, or both go ahead."""
        outcomes: list[Any] = []
        for world in self.worlds:
            try:
                outcomes.append(("ok", self.run(act(world))))
            except ToolError as refused:
                outcomes.append(("refused", str(refused)))
        kinds = [kind for kind, _ in outcomes]
        assert kinds[0] == kinds[1], f"the stores disagree: {outcomes}"
        return outcomes

    # A drafting run over the posted messages.

    def start_run(self) -> None:
        for world in self.worlds:
            self.run(self._start(world))

    async def _start(self, world: World) -> None:
        if world.grant is not None:  # one run at a time in a thread, as the lock allows
            await world.store.finish_run(world.grant.run_id, RunStatus.SUCCEEDED)
        run = await world.store.enqueue_run(RunKind.DRAFTING, CH, ROOT)
        # Other runs, such as an execution an approval queued, are claimed and ended:
        # the machine plays only drafting runs.
        while (claimed := await world.store.claim_next_run(max_running=64)) is not None:
            if claimed.id == run.id:
                break
            await world.store.finish_run(claimed.id, RunStatus.SUCCEEDED)
        assert claimed is not None and claimed.id == run.id
        world.grant = Grant(run.id, RunKind.DRAFTING, CH, thread_ts=ROOT)
        await world.reads["get_work"].handler(world.grant, NoArguments())
        if ROOT in world.posted:
            sha = __import__("hashlib").sha256(Files().texts[FILE]).hexdigest()
            await world.store.record_tool_call(
                ToolCallRecord(
                    run_id=run.id,
                    tool=SERVED_FILE,
                    args_hash=FILE,
                    channel_id=CH,
                    thread_ts=ROOT,
                    result_json=json.dumps({"sha256": sha, "ts": ROOT}),
                    called_at=START,
                )
            )

    async def _work(self, world: World) -> Work:
        assert world.grant is not None
        return await world.reads["get_work"].handler(world.grant, NoArguments())

    @rule(index=st.integers(0, len(MESSAGES) - 1))
    def post(self, index: int) -> None:
        for world in self.worlds:
            world.posted.add(MESSAGES[index].ts)
        self.start_run()

    # The agent's placements.

    def _posted(self) -> list[str]:
        return sorted(self.worlds[0].posted)

    async def _topic_by_title(self, world: World, title: str) -> str | None:
        for record in await world.store.channel_topics(CH):
            if record.topic.title == title:
                return record.topic.id
        return None

    def _titles(self) -> list[str]:
        return list(self.created)

    @precondition(lambda self: bool(self.worlds[0].posted))
    @rule(data=st.data(), split=st.booleans())
    def link_new(self, data: st.DataObject, split: bool) -> None:
        ts = data.draw(st.sampled_from(self._posted()))
        self.titles += 1
        title = f"Topic {self.titles}"
        decision = "also_topic" if split else "new_topic"

        async def act(world: World) -> Any:
            assert world.tools is not None and world.grant is not None
            args = LinkMessageArgs.model_validate(
                {"ts": ts, "decision": decision, "title": title, "confidence": 0.9}
            )
            return await world.tools.link_message(world.grant, args)

        self.both(act)
        made = [self.run(self._topic_by_title(world, title)) for world in self.worlds]
        assert (made[0] is None) == (made[1] is None), "the stores disagree on a new topic"
        if made[0] is not None:
            self.created.append(title)

    @precondition(lambda self: bool(self.worlds[0].posted) and bool(self.created))
    @rule(data=st.data())
    def link_existing(self, data: st.DataObject) -> None:
        ts = data.draw(st.sampled_from(self._posted()))
        title = data.draw(st.sampled_from(self._titles()))

        async def act(world: World) -> Any:
            assert world.tools is not None and world.grant is not None
            topic_id = await self._topic_by_title(world, title)
            args = LinkMessageArgs.model_validate(
                {"ts": ts, "decision": "existing_topic", "topic_id": topic_id, "confidence": 0.9}
            )
            return await world.tools.link_message(world.grant, args)

        self.both(act)

    @precondition(lambda self: bool(self.worlds[0].posted))
    @rule(data=st.data())
    def agent_chatter(self, data: st.DataObject) -> None:
        ts = data.draw(st.sampled_from(self._posted()))

        async def act(world: World) -> Any:
            assert world.tools is not None and world.grant is not None
            args = LinkMessageArgs.model_validate(
                {"ts": ts, "decision": "chatter", "confidence": 0.9}
            )
            return await world.tools.link_message(world.grant, args)

        self.both(act)

    # Proposing and revising, as the agent.

    @precondition(lambda self: bool(self.created))
    @rule(
        data=st.data(),
        attach=st.booleans(),
        assignee=st.sampled_from(["none", "volunteer", "named"]),
    )
    def propose(self, data: st.DataObject, attach: bool, assignee: str) -> None:
        title = data.draw(st.sampled_from(self._titles()))
        sources = data.draw(
            st.lists(st.sampled_from(self._posted()), min_size=1, max_size=3, unique=True)
            if self.worlds[0].posted
            else st.just([ROOT])
        )
        assignees: list[str] = []
        reasons: list[dict[str, Any]] = []
        if assignee == "volunteer" and "1792310460.000100" in sources:
            assignees = ["octo-alex"]
            reasons = [{"login": "octo-alex", "why": "volunteered", "source_ts": sources[0]}]
            reasons[0]["source_ts"] = "1792310460.000100"
        if assignee == "named" and "1792310520.000100" in sources:
            assignees = ["octo-chris"]
            reasons = [
                {"login": "octo-chris", "why": "named_in_text", "source_ts": "1792310520.000100"}
            ]

        async def act(world: World) -> Any:
            assert world.tools is not None and world.grant is not None
            topic_id = await self._topic_by_title(world, title)
            args = ProposeIssueArgs.model_validate(
                {
                    "topic_id": topic_id,
                    "repo": "acme-co/web",
                    "title": f"Work for {title}",
                    "body": "Do the thing.",
                    "labels": ["enhancement"],
                    "assignees": assignees,
                    "assignee_reasons": reasons,
                    "sources": [{"channel_id": CH, "ts": ts} for ts in sources],
                    "attachments": [FILE] if attach else [],
                    "confidence": 0.8,
                }
            )
            result: Any = await world.tools.propose_issue(world.grant, args)
            world.agent_reasons[result.proposal_id] = {
                r["login"].casefold(): (r["why"], r["source_ts"]) for r in reasons
            }
            await world.store.set_card_ts(result.proposal_id, f"17923{result.proposal_id[-5:]}.1")
            return result.proposal_id

        self.both(act)

    async def _pending_drafts(self, world: World) -> list[Any]:
        work = await self._work(world)
        assert work.conversation is not None
        return [t.pending for t in work.conversation.topics.topics if t.pending is not None]

    @rule(data=st.data(), with_request=st.booleans())
    def revise(self, data: st.DataObject, with_request: bool) -> None:
        if with_request:
            self.change_requests += 1
            ts = f"17923106{self.change_requests:02d}.000900"
            for world in self.worlds:
                world.posted.add(ts)
                world.extra_authors[ts] = ALICE
            self.start_run()
        offered = self.run(self._pending_drafts(self.worlds[0]))
        if not offered:
            return
        pick = data.draw(st.integers(0, len(offered) - 1))
        source = data.draw(st.sampled_from(self._posted())) if self.worlds[0].posted else ROOT

        async def act(world: World) -> Any:
            assert world.tools is not None and world.grant is not None
            pending = (await self._pending_drafts(world))[pick]
            record = await world.store.get_proposal(pending.proposal_id)
            assert record is not None
            shown = {
                r.login.casefold(): r.model_dump(mode="json")
                for r in getattr(pending, "assignee_reasons", None) or []
            }
            mine = world.agent_reasons.get(record.id, {})
            assignees, reasons = [], []
            for login in pending.assignees if hasattr(pending, "assignees") else []:
                key = login.casefold()
                if key in shown:
                    assignees.append(login)
                    reasons.append(shown[key])
                elif key in mine:
                    why, at = mine[key]
                    assignees.append(login)
                    reasons.append({"login": login, "why": why, "source_ts": at})
                # Neither shown nor its own: the agent can't give a reason, so it's left off.
            draft = record.proposal
            if isinstance(draft, IssueProposal):
                issue = {
                    "title": draft.title,
                    "body": draft.body + "\n\nAnd more.",
                    "labels": draft.labels,
                    "assignees": assignees,
                    "assignee_reasons": reasons,
                }
                payload: dict[str, Any] = {"issue": issue}
            else:
                changes = draft.changes.model_dump(mode="json")
                changes["comment"] = "A follow-up."
                payload = {"changes": changes}
            args = ReviseProposalArgs.model_validate(
                {
                    "proposal_id": record.id,
                    "base_revision": pending.revision,
                    "sources": [{"channel_id": CH, "ts": source}],
                    "confidence": 0.8,
                    **payload,
                }
            )
            return await world.tools.revise_proposal(world.grant, args)

        self.both(act)

    # People's decisions.

    @precondition(lambda self: bool(self.worlds[0].posted))
    @rule(data=st.data(), what=st.sampled_from(["skip", "re", "re_summary"]))
    def person_decides(self, data: st.DataObject, what: str) -> None:
        ts = data.draw(st.sampled_from(self._posted()))
        title = data.draw(st.sampled_from(self._titles())) if self.created else None
        if what != "skip" and title is None:
            return
        if what == "re_summary":
            self.summary_replies += 1
            ts = f"17923200{self.summary_replies:02d}.000500"

        async def act(world: World) -> Any:
            ref = MessageRef(
                channel_id=CH, ts=ts, thread_ts=SUMMARY if what == "re_summary" else ROOT
            )
            if what == "skip":
                return await world.store.mark_chatter(ref, by="person")
            assert title is not None
            topic_id = await self._topic_by_title(world, title)
            assert topic_id is not None
            return await world.store.link_message(ref, topic_id, by="person")

        self.both(act)

    @rule(data=st.data(), file_it=st.booleans())
    def approve(self, data: st.DataObject, file_it: bool) -> None:
        pending = [
            r.id
            for r in self.run(self.worlds[0].store.thread_proposals(CH, ROOT))
            if r.state is ProposalState.PENDING and r.run_id != SELF_ADD_RUN
        ]
        if not pending:
            return
        proposal_id = data.draw(st.sampled_from(sorted(pending)))

        async def act(world: World) -> Any:
            await world.store.transition(proposal_id, ProposalState.APPROVED, approved_by="auto")
            if not file_it:
                return None
            await world.store.transition(proposal_id, ProposalState.EXECUTING)
            record = await world.store.get_proposal(proposal_id)
            assert record is not None
            number = 40 + int(proposal_id, 16) % 50
            if isinstance(record.proposal, UpdateProposal):
                number = record.proposal.issue_number
            url = f"https://github.com/acme-co/web/issues/{number}"
            await world.store.transition(
                proposal_id, ProposalState.APPLIED, result=ExecutionResult(issue_url=url)
            )
            if record.proposal.topic_id is not None and isinstance(record.proposal, IssueProposal):
                await world.store.set_topic_issue(record.proposal.topic_id, "acme-co/web", number)
            return None

        self.both(act)

    @rule(data=st.data(), user=st.sampled_from([ALICE, BOB]))
    def add_me(self, data: st.DataObject, user: str) -> None:
        records = self.run(self.worlds[0].store.thread_proposals(CH, ROOT))
        cards = [r.id for r in records if r.state is ProposalState.PENDING and r.card_ts]
        filed = [r.id for r in records if r.state is ProposalState.APPLIED]
        choices = [("card", p) for p in cards] + [("filed", p) for p in filed]
        if not choices:
            return
        where, proposal_id = data.draw(st.sampled_from(sorted(choices)))

        async def act(world: World) -> Any:
            assert world.add_me is not None
            record = await world.store.get_proposal(proposal_id)
            assert record is not None
            before = {r.id for r in await world.store.thread_proposals(CH, ROOT)}
            if where == "card":
                assert record.card_ts is not None
                click = _click(ACTION_ADD_ME, f"{proposal_id}:me", user, record.card_ts, None)
                outcome = await world.add_me.on_card(click)
                login = LOGINS[user][0].casefold()
                after = await world.store.get_proposal(proposal_id)
                if after is not None and any(
                    r.why == "added_on_card" and r.login.casefold() == login
                    for r in getattr(after.proposal, "assignee_reasons", [])
                ):
                    world.card_adds.setdefault(proposal_id, set()).add(login)
                return outcome
            click = _click(ACTION_ADD_ME_FILED, proposal_id, user, "1792399999.000100", ROOT)
            outcome = await world.add_me.on_filed(click)
            for r in await world.store.thread_proposals(CH, ROOT):
                if r.id not in before and r.run_id == SELF_ADD_RUN:
                    world.self_adds[r.id] = _snapshot_self_add(r)
            return outcome

        self.both(act)

    @rule()
    def rebuild(self) -> None:
        """Rebuild from GitHub's view: each filed issue's thread, its posted messages."""

        async def act(world: World) -> Any:
            issues: list[tuple[str, int]] = []
            for record in await world.store.thread_proposals(CH, ROOT):
                if record.state is ProposalState.APPLIED and record.result is not None:
                    url = record.result.issue_url or ""
                    number = int(url.rsplit("/", 1)[-1])
                    if ("acme-co/web", number) not in issues:
                        issues.append(("acme-co/web", number))
            if not issues:
                return None
            plan = ChannelPlan(
                CH,
                threads=[
                    ThreadPlan(
                        ROOT, issues=issues, messages=[(ts, 0) for ts in sorted(world.posted)]
                    )
                ],
            )
            return await rebuild_apply(world.store, POLICY, plan, BOT)

        self.both(act)

    # Invariants, after every step, on each store.

    @invariant()
    def no_pending_draft_on_an_empty_topic(self) -> None:
        if not checking("no_pending_draft_on_an_empty_topic"):
            return
        for world in self.worlds:
            for record in self.run(world.store.thread_proposals(CH, ROOT)):
                topic_id = record.proposal.topic_id
                if (
                    record.state is not ProposalState.PENDING
                    or record.draft_cleared_at is not None
                    or topic_id is None
                    or record.run_id == SELF_ADD_RUN
                ):
                    continue
                topic = self.run(world.store.get_topic(topic_id))
                assert topic is not None and topic.topic.messages, (
                    f"pending draft {record.id} sits on topic {topic_id}, which has no messages"
                )

    @invariant()
    def an_add_me_request_is_changed_only_by_its_requester(self) -> None:
        if not checking("an_add_me_request_is_changed_only_by_its_requester"):
            return
        for world in self.worlds:
            for proposal_id, snapshot in world.self_adds.items():
                record = self.run(world.store.get_proposal(proposal_id))
                assert record is not None
                if record.draft_cleared_at is not None:
                    continue  # superseded or done: its text is gone, which is fine
                assert _snapshot_self_add(record) == snapshot, (
                    f"Add me request {proposal_id} was changed by someone else's action"
                )

    @invariant()
    def a_card_add_is_never_dropped_by_the_agent(self) -> None:
        if not checking("a_card_add_is_never_dropped_by_the_agent"):
            return
        for world in self.worlds:
            for proposal_id, logins in world.card_adds.items():
                record = self.run(world.store.get_proposal(proposal_id))
                if record is None or record.state is not ProposalState.PENDING:
                    continue
                kept = {a.casefold() for a in getattr(record.proposal, "assignees", [])}
                assert logins <= kept, f"a person's Add me left draft {proposal_id}"

    @invariant()
    def every_assignee_has_a_valid_reason_and_names_keep_their_match(self) -> None:
        if not checking("every_assignee_has_a_valid_reason_and_names_keep_their_match"):
            return
        for world in self.worlds:
            for record in self.run(world.store.thread_proposals(CH, ROOT)):
                draft = record.proposal
                if record.draft_cleared_at is not None or not isinstance(draft, IssueProposal):
                    continue
                reasons = {r.login.casefold(): r for r in draft.assignee_reasons}
                for login in draft.assignees:
                    assert login.casefold() in reasons, f"{login} on {record.id} has no reason"
                for reason in draft.assignee_reasons:
                    if reason.why == "named_in_text":
                        assert reason.matched, (
                            f"{reason.login}'s named_in_text on {record.id} lost its match"
                        )

    @invariant()
    def at_most_one_pending_create_per_topic(self) -> None:
        if not checking("at_most_one_pending_create_per_topic"):
            return
        for world in self.worlds:
            seen: dict[str, str] = {}
            for record in self.run(world.store.thread_proposals(CH, ROOT)):
                if record.state is not ProposalState.PENDING:
                    continue
                if not isinstance(record.proposal, IssueProposal):
                    continue
                topic_id = record.proposal.topic_id
                if topic_id is None:
                    continue
                assert topic_id not in seen, (
                    f"two pending creates on topic {topic_id}: {seen[topic_id]}, {record.id}"
                )
                seen[topic_id] = record.id

    @invariant()
    def an_attached_file_comes_from_the_proposals_own_sources(self) -> None:
        if not checking("an_attached_file_comes_from_the_proposals_own_sources"):
            return
        for world in self.worlds:
            for record in self.run(world.store.thread_proposals(CH, ROOT)):
                draft = record.proposal
                if not isinstance(draft, IssueProposal) or not draft.attached:
                    continue
                sources = {s.ts for s in draft.sources}
                assert ROOT in sources, f"{record.id} attaches the root's file without it"

    @invariant()
    def a_split_message_doesnt_carry_one_file_into_two_topics(self) -> None:
        if not checking("a_split_message_doesnt_carry_one_file_into_two_topics"):
            return
        for world in self.worlds:
            holders: dict[str, str] = {}
            for record in self.run(world.store.thread_proposals(CH, ROOT)):
                draft = record.proposal
                if record.state in (ProposalState.REJECTED, ProposalState.SUPERSEDED):
                    continue
                if not isinstance(draft, IssueProposal) or not draft.attached:
                    continue
                topic = draft.topic_id or ""
                other = holders.setdefault(FILE, topic)
                assert other == topic, f"one file attached on topics {other} and {topic}"

    @invariant()
    def both_stores_agree(self) -> None:
        if not checking("both_stores_agree"):
            return
        views = [self.run(_view(world.store)) for world in self.worlds]
        assert views[0] == views[1], "MemoryStore and SqliteStore differ"


async def _view(store: Store) -> Any:
    """What both stores must agree on, with their own topic ids replaced by titles."""
    titles = {r.topic.id: r.topic.title for r in await store.channel_topics(CH)}
    messages = {}
    for record in await store.channel_topics(CH):
        messages[record.topic.title] = sorted(m.ts for m in record.topic.messages)
    proposals = {}
    for record in await store.thread_proposals(CH, ROOT):
        draft = record.proposal
        proposals[record.id] = (
            record.state.value,
            titles.get(draft.topic_id or ""),
            # The draft names its topic by each store's own id: compare it by title.
            content_hash(draft.model_copy(update={"topic_id": titles.get(draft.topic_id or "")}))
            if record.draft_cleared_at is None
            else "cleared",
            record.revision,
        )
    return messages, proposals


TestStoreMachine = StoreMachine.TestCase
