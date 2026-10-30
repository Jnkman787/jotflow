"""Everything `jotflow run` runs besides the Slack connection (#55).

:class:`Orchestrator` opens the store, serves the whiteboard tools over MCP,
feeds accepted messages to the debouncer, and drives runs through
:class:`~jotflow.orchestrator.runs.RunLifecycle` and
:class:`~jotflow.orchestrator.launch.RunSupervisor`.

**Start order:**

1. Open the store.
2. Recover interrupted runs, before anything else touches runs.
3. Build the tools and start the MCP server.
4. Re-arm threads still waiting out their quiet window (#130).
5. Start the supervisor and the debouncer's flush loop.

**Stop order is the reverse:**

1. Stop the debouncer's loop.
2. Stop the supervisor, which tells the runners to kill live runs.
3. Revoke every token, so no agent can act while the MCP server shuts down.
4. Stop the MCP server.
5. Close the store.

Runs still running stay so in the store, and the next start's recovery marks
them interrupted and queues them again.

Each run kind is on only when ``[agent] preset`` is set in jotflow.toml and
JotFlow holds that kind's runner secret, decided once at startup with one log
line for each kind that is off. A kind that is off never starts: its runs stay
queued and its proposals stay approved. With drafting off, nothing is queued
from Slack. With every kind off, no token is issued and the MCP server refuses
every call, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from slack_sdk.web.async_client import AsyncWebClient

from jotflow.board_catalog import BoardCatalog
from jotflow.config import Settings
from jotflow.models import (
    ChannelMode,
    DiscoveredRepo,
    FoundBoard,
    IssueDetails,
    ProposalRecord,
    RunKind,
    expected_runner_preset,
)
from jotflow.orchestrator.add_me import AddMe, AddMeSlack, BoltAddMeSlack, PersonChecks
from jotflow.orchestrator.approval import ApprovalHandler, ApprovalSlack, BoltApprovalSlack
from jotflow.orchestrator.boards import BoardAdder
from jotflow.orchestrator.card_edits import CardEdits
from jotflow.orchestrator.card_sync import (
    SYNC_SECONDS,
    BoltPeople,
    CardSync,
    People,
    card_context,
)
from jotflow.orchestrator.cards import CardContext, CardPoster, CardPublisher, SlackCardPoster
from jotflow.orchestrator.debounce import Debouncer
from jotflow.orchestrator.help import BoltHelpSlack, HelpHandler, HelpSlack
from jotflow.orchestrator.identity import LinkCommand, Notifier, PeopleLookup, SlackNote
from jotflow.orchestrator.ingest import Accepted, BotIdentity, Decision
from jotflow.orchestrator.keyword_handler import BoltKeywordSlack, KeywordHandler, KeywordSlack
from jotflow.orchestrator.launch import GithubBusy, RunnerClient, RunnerError, RunSupervisor
from jotflow.orchestrator.modes import AutoApprover, BoltModeSlack, ModeCommand, ModeSlack
from jotflow.orchestrator.notices import GiveUpNotice
from jotflow.orchestrator.packet import BoltSlackReader, SlackWorkSource
from jotflow.orchestrator.people import SlackAttribution, SlackPeople
from jotflow.orchestrator.questions import (
    BoltQuestionSlack,
    QuestionHandler,
    QuestionSlack,
    is_ask,
)
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.orchestrator.slack import SlackThreadPoster
from jotflow.orchestrator.slack_files import BoltFileSlack
from jotflow.orchestrator.summary import BoltSummarySlack, WeeklySummaries
from jotflow.orchestrator.thread_notice import BoltThreadSlack, ThreadNotice, ThreadSlack
from jotflow.orchestrator.verify import Verifier, VerifyCommand
from jotflow.policy import Policy
from jotflow.repo_catalog import RepoCatalog
from jotflow.store import SqliteStore, Store
from jotflow.whiteboard.doc_tools import (
    DocReads,
    DocSource,
    doc_tools,
    probe_docs,
)
from jotflow.whiteboard.operator_tokens import GateTokens, OperatorTokens
from jotflow.whiteboard.read_tools import IssueReader, IssueUnavailable, LabelChecker, read_tools
from jotflow.whiteboard.tokens import TokenCheck, TokenRegistry
from jotflow.whiteboard.tools import ToolSpec
from jotflow.whiteboard.write_tools import write_tools

log = logging.getLogger("jotflow.orchestrator")

FLUSH_SECONDS = 1.0


class RunnerIssues:
    """get_issue's reader: the drafting runner, with every failure as "unavailable" (#273).

    A rate limit or a runner fault never reads as "no such issue".
    """

    def __init__(self, runner: RunnerClient) -> None:
        self._runner = runner

    async def issue(self, owner: str, name: str, number: int) -> IssueDetails | None:
        try:
            return await self._runner.issue(owner, name, number)
        except GithubBusy:
            log.info("get_issue: GitHub couldn't be asked right now")
            raise IssueUnavailable from None
        except RunnerError as failed:
            log.warning("get_issue: the runner failed: %s", failed)
            raise IssueUnavailable from None


class Server(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


ServerFactory = Callable[[Settings, TokenCheck, Sequence[ToolSpec]], Server]
StoreOpener = Callable[[str], Awaitable[Store]]


async def open_sqlite(path: str) -> Store:
    # One writer at a time: a rebuild, or a second JotFlow, can't share the store.
    # Unlocked, with a warning, where the filesystem can't lock: as before the lock.
    return await SqliteStore.open(path, exclusive=True, unlocked_ok=True)


CONVERSATION_KINDS = frozenset({RunKind.DRAFTING, RunKind.QUESTION})


@dataclass(frozen=True)
class RunKinds:
    """Which run kinds may start, decided once at startup, and why the others may not."""

    on: frozenset[RunKind]
    off: tuple[str, ...]


def decide_run_kinds(settings: Settings, policy: Policy) -> RunKinds:
    """Each kind is on only with a preset and its own runner's secret.

    A kind that is off never starts: its runs stay queued and its proposals
    stay approved, rather than failing for want of a runner.
    """
    if policy.agent.preset is None:
        return RunKinds(
            frozenset(), ("agent runs are off: [agent] preset is not set in jotflow.toml",)
        )
    on: set[RunKind] = set()
    off: list[str] = []
    if settings.runner_secret is None:
        off.append("drafting and question runs are off: JOTFLOW_RUNNER_SECRET is not set")
    else:
        on |= CONVERSATION_KINDS
    if settings.runner_exec_secret is None:
        off.append("execution runs are off: JOTFLOW_RUNNER_EXEC_SECRET is not set")
    else:
        on.add(RunKind.EXECUTION)
    return RunKinds(frozenset(on), tuple(off))


# Runners start alongside JotFlow, so give them a moment to answer.
INFO_ATTEMPTS = 10
INFO_DELAY_SECONDS = 3.0
# A runner whose name doesn't resolve isn't running: Docker's DNS knows a container's
# name from the moment it starts. So stop asking after this long, not after every
# attempt (#234). A runner that resolves but isn't listening yet keeps every attempt.
UNRESOLVED_BUDGET_SECONDS = 15.0

# Each runner serves exactly one group of kinds, asked about by its first kind.
RUNNER_GROUPS: tuple[tuple[str, frozenset[RunKind]], ...] = (
    ("drafting and question", CONVERSATION_KINDS),
    ("execution", frozenset({RunKind.EXECUTION})),
)


async def check_runners(
    client: RunnerClient,
    kinds: RunKinds,
    preset: str,
    *,
    attempts: int = INFO_ATTEMPTS,
    delay: float = INFO_DELAY_SECONDS,
) -> RunKinds:
    """Ask each runner that is on what it starts, with its own secret.

    A runner that will not answer, or starts another preset or other kinds than
    jotflow.toml expects, turns its kinds off with one reason. Their runs stay
    queued, as for a missing secret.
    """
    on = set(kinds.on)
    off = list(kinds.off)
    for label, group in RUNNER_GROUPS:
        # Asked whenever any of its kinds is on, so one kind off can't skip the check.
        if not group & on:
            continue
        reason = await _runner_mismatch(client, group, preset, attempts=attempts, delay=delay)
        if reason is not None:
            on -= group
            off.append(f"{label} runs are off: {reason}")
    return RunKinds(frozenset(on), tuple(off))


async def _runner_mismatch(
    client: RunnerClient,
    group: frozenset[RunKind],
    preset: str,
    *,
    attempts: int,
    delay: float,
    unresolved_budget: float = UNRESOLVED_BUDGET_SECONDS,
) -> str | None:
    kind = min(group, key=lambda k: list(RunKind).index(k))
    endpoint = client.endpoint_for(kind)
    where = endpoint.url if endpoint is not None else "its runner"
    problem = "no attempt was made"
    started = asyncio.get_running_loop().time()
    for attempt in range(max(attempts, 1)):
        try:
            info = await client.info(kind)
        except RunnerError as exc:
            problem = str(exc)
            if exc.retry is None:
                # A refusal, such as a wrong secret or a public address (#234),
                # will not fix itself.
                break
            elapsed = asyncio.get_running_loop().time() - started
            if exc.retry == "unresolved" and elapsed + delay > unresolved_budget:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
            continue
        expected = expected_runner_preset(group, preset)
        if info.preset != expected:
            if expected == preset:
                return (
                    f"the runner at {where} starts preset {info.preset}, "
                    f"but jotflow.toml's [agent] preset is {preset}"
                )
            return (
                f"the runner at {where} starts preset {info.preset}, but jotflow.toml's "
                f"[agent] preset {preset} does not serve {kind.value} runs; "
                f"that runner must start {expected}"
            )
        if frozenset(info.kinds) != group:
            served = ", ".join(k.value for k in info.kinds) or "nothing"
            return f"the runner at {where} serves {served}"
        return None
    return f"the runner at {where} did not answer: {problem}"


class Orchestrator:
    """The store, the MCP server, the debouncer, and the run supervisor, in order."""

    def __init__(
        self,
        settings: Settings,
        policy: Policy,
        client: AsyncWebClient,
        identity: BotIdentity,
        *,
        make_server: ServerFactory,
        open_store: StoreOpener = open_sqlite,
        runner: RunnerClient | None = None,
        keyword_slack: KeywordSlack | None = None,
        help_slack: HelpSlack | None = None,
        question_slack: QuestionSlack | None = None,
        card_poster: CardPoster | None = None,
        people: People | None = None,
        card_seconds: float = SYNC_SECONDS,
        approval_slack: ApprovalSlack | None = None,
        link_people: PeopleLookup | None = None,
        link_notifier: Notifier | None = None,
        mode_slack: ModeSlack | None = None,
        thread_slack: ThreadSlack | None = None,
        add_me_slack: AddMeSlack | None = None,
        flush_seconds: float = FLUSH_SECONDS,
        tick_seconds: float | None = None,
        info_attempts: int = INFO_ATTEMPTS,
        info_delay: float = INFO_DELAY_SECONDS,
    ) -> None:
        self._info_attempts = info_attempts
        self._info_delay = info_delay
        self._settings = settings
        self._policy = policy
        self._client = client
        self._identity = identity
        self._make_server = make_server
        self._open_store = open_store
        self._runner = runner
        self._keyword_slack = keyword_slack
        self._help_slack = help_slack
        self._question_slack = question_slack
        self._questions: QuestionHandler | None = None
        self._help: HelpHandler | None = None
        self._card_poster = card_poster
        self._people = people
        self._card_seconds = card_seconds
        self.cards: CardSync | None = None
        self._approval_slack = approval_slack
        self._link_people = link_people
        self._link_notifier = link_notifier
        self.approval: ApprovalHandler | None = None
        self.edits: CardEdits | None = None
        self._add_me_slack = add_me_slack
        self.add_me: AddMe | None = None
        self._mode_slack = mode_slack
        self._thread_slack = thread_slack
        self.threads: ThreadNotice | None = None
        self.modes: ModeCommand | None = None
        self.links: LinkCommand | None = None
        self.verify: VerifyCommand | None = None
        self.auto: AutoApprover | None = None
        self._keywords: KeywordHandler | None = None
        self._flush_seconds = flush_seconds
        self._tick_seconds = tick_seconds
        self.registry = TokenRegistry()
        self.kinds = decide_run_kinds(settings, policy)
        self._store: Store | None = None
        self._server: Server | None = None
        self._debouncer: Debouncer | None = None
        self._supervisor: RunSupervisor | None = None
        self._stop = asyncio.Event()
        self.operators: OperatorTokens | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # Listed repos, and those a channel's scopes find through the runners (#236).
        self.repos = RepoCatalog(policy, lister=self._list_repos, access=self._write_reachable)
        # Listed boards, and those a channel's board scopes find (#309). Its ``off``
        # holds listed boards the exec runner's write token can't update.
        self.boards = BoardCatalog(
            policy,
            finder=self._find_boards,
            resolve=self._person_id,
            access=self._board_writable,
        )

    async def _list_repos(self, owner: str) -> list[DiscoveredRepo]:
        if self._runner is None:
            raise RunnerError("no runner to ask")
        return await self._runner.repos(owner)

    async def _find_boards(
        self,
        owner: str,
        numbers: Sequence[int],
        marker: str | None,
        creator_ids: Sequence[int],
        exclude: Sequence[str],
    ) -> list[FoundBoard]:
        if self._runner is None or RunKind.EXECUTION not in self.kinds.on:
            raise RunnerError("no runner to ask")  # nothing could add to a found board
        return await self._runner.find_boards(
            owner, numbers=numbers, marker=marker, creator_ids=creator_ids, exclude=exclude
        )

    async def _person_id(self, login: str) -> int | None:
        # A board creator's account id, by #316's account lookup (#309 PR 2).
        if self._runner is None:
            raise RunnerError("no runner to ask")
        account = await self._runner.account(login)
        return account.id if account.account == "user" else None

    async def _board_writable(self, owner: str, number: int) -> bool:
        if self._runner is None:
            raise RunnerError("no runner to ask")
        return await self._runner.board_access(owner, number)

    async def _write_reachable(self, owner: str, name: str) -> bool:
        if self._runner is None:
            raise RunnerError("no runner to ask")
        return await self._runner.write_reachable(owner, name)

    @property
    def store(self) -> Store:
        if self._store is None:
            raise RuntimeError("the orchestrator has not started")
        return self._store

    def _verifier(self) -> Verifier | None:
        """The drafting runner, when it's on, for checking a GitHub link's code (#195).

        It serves drafting and questions, so either kind being on is enough: the
        check is anonymous, so a codex drafting refusal (#209) doesn't touch it.
        """
        if self._runner is None or not self.kinds.on & CONVERSATION_KINDS:
            return None
        return self._runner

    def _label_checker(self) -> LabelChecker | None:
        """The drafting runner, when it's on, for an [issues] label check before a write
        (#306 follow-up). Without it the label stays, so a missing one stops the write."""
        if self._runner is None or RunKind.DRAFTING not in self.kinds.on:
            return None
        return self._runner

    def _person_checks(self) -> PersonChecks | None:
        """The drafting runner, when drafting is on, for Add me's GitHub checks (#305)."""
        if self._runner is None or RunKind.DRAFTING not in self.kinds.on:
            return None
        return self._runner

    def _issue_reader(self) -> IssueReader | None:
        """The drafting runner, when question runs are on, for get_issue (#273)."""
        if self._runner is None or RunKind.QUESTION not in self.kinds.on:
            return None
        return RunnerIssues(self._runner)

    def _doc_source(self) -> DocSource | None:
        """The drafting runner, when it's on, for reading repo docs (#209)."""
        if self._runner is None or RunKind.DRAFTING not in self.kinds.on:
            return None
        return self._runner

    @property
    def runs_on(self) -> bool:
        return bool(self.kinds.on)

    async def start(self) -> None:
        """Start everything in order. On failure, stops what had started, then raises."""
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        self._store = await self._open_store(self._settings.database_path)
        poster = SlackThreadPoster(self._client)
        lifecycle = RunLifecycle(
            self._store, self.registry, on_gave_up=GiveUpNotice(poster, self._policy)
        )
        await lifecycle.recover()
        # One subscriber draws every card, outside any MCP write path (#152).
        people = self._people or BoltPeople(self._client)
        # One auth.test for the whole process: the team check and the workspace URL.
        slack_people = SlackPeople(self._client)
        self.cards = CardSync(
            self._store,
            self._policy,
            CardPublisher(self._store, self._card_poster or SlackCardPoster(self._client)),
            people,
            repos=self.repos,
            # A revision that lost a race drafts its thread again once settled (#304).
            arm_thread=self._arm_thread,
            boards=self.boards,
        )
        # Auto mode approves before each card pass, so its card waits for the write (#28).
        self.auto = AutoApprover(self._store, self.channel_mode, self._policy.issues)
        # Once an issue is written, its thread hears where it went (#32).
        self.threads = ThreadNotice(
            self._store,
            self._policy,
            self._thread_slack or BoltThreadSlack(self._client),
            repos=self.repos,
            # Approved boards are added by the exec runner's fixed call (#309).
            boards=BoardAdder(self._store, self._policy, _RunnerBoards(self), boards=self.boards),
        )
        # Each person links only their own GitHub login (#35); approvers and
        # identity admins can remove someone else's (#173).
        self.links = LinkCommand(
            self._store,
            self._link_people or slack_people,
            policy=self._policy,
            names=people,
            notifier=self._link_notifier or SlackNote(self._client),
        )
        # Proving control of a GitHub login (#195), checked by the drafting runner.
        self.verify = VerifyCommand(
            self._store,
            self._link_people or slack_people,
            self._verifier,
            self._link_notifier or SlackNote(self._client),
        )
        self.modes = ModeCommand(
            self._store,
            self._policy,
            self._mode_slack or BoltModeSlack(self._client, slack_people, people),
        )
        store, policy, repos = self._store, self._policy, self.repos

        # Who the issue will credit, looked up the way the footer does (#307 review).
        credits = SlackAttribution(slack_people, people.name)

        async def drawn_with(record: ProposalRecord) -> CardContext:
            # The context the card loop draws with, so length rules agree (#34).
            return await card_context(
                policy, store, people, record, repos, credits, boards=self.boards
            )

        self._tasks.append(asyncio.create_task(self._card_loop(self.cards), name="jotflow-cards"))
        # The approval gate answers the card's buttons whether or not runs are on;
        # an approved proposal's execution run waits in the queue until they are.
        approval_slack = self._approval_slack or BoltApprovalSlack(
            self._client, people=slack_people
        )
        self.approval = ApprovalHandler(
            self._store,
            self._policy,
            approval_slack,
            refresh=self.cards.refresh,
            arm_thread=self._arm_thread,
            context_for=drawn_with,
            repos=self.repos,
            redraw=self.cards.redraw,
        )
        # The card's repo dropdown and assign buttons pass the same gate (#160).
        self.edits = CardEdits(
            self._store,
            self._policy,
            self.approval.guarded,
            approval_slack,
            refresh=self.cards.refresh,
            repos=self.repos,
            boards=self.boards,
        )
        # Add me on a card or a filed issue's reply, through the same gate (#305).
        self.add_me = AddMe(
            self._store,
            self._policy,
            self.approval,
            self._add_me_slack or BoltAddMeSlack(self._client),
            self._person_checks,
            refresh=self.cards.refresh,
            repos=self.repos,
        )
        # Keywords are a person's decisions, stored even while agent runs are off.
        self._keywords = KeywordHandler(
            self._store,
            self._policy,
            self._keyword_slack or BoltKeywordSlack(self._client, people=slack_people),
            repos=self.repos,
        )
        self._help = HelpHandler(
            self._store, self._policy, self._help_slack or BoltHelpSlack(self._client)
        )
        # Slack files (#333): JotFlow's own reader, used only where a channel allows it.
        files = BoltFileSlack(self._client)
        source = SlackWorkSource(
            BoltSlackReader(self._client),
            self._store,
            bot=self._identity,
            files=files,
            attachments_for=self._policy.attachments_for,
        )
        # Repo docs for drafting (#209): no tools at all unless a repo sets read_paths.
        # The runner client is made below, so the tools look it up when called.
        doc_reads = DocReads(self._store)
        attribution = SlackAttribution(slack_people, people.name)
        tools = (
            *read_tools(
                self._store,
                self._policy,
                source,
                attribution=attribution,
                repos=self.repos,
                issues=self._issue_reader,
                boards=self.boards,
                labels=self._label_checker,
                files=files,
            ),
            *write_tools(
                self._store,
                self._policy,
                poster,
                repos=self.repos,
                docs=doc_reads,
                boards=self.boards,
                # Who wrote a revision's new messages, as approval reads them (#304).
                authors=attribution.authors,
                files=files,
                mentions=attribution.mentions,
                texts=attribution.text,
                names=attribution.names,
            ),
            *doc_tools(self._store, self._policy, self._doc_source, doc_reads),
        )
        # The weekly summary (#308), for channels that set one, whether or not runs are on.
        self.summaries: WeeklySummaries | None = None
        self.summary_slack = BoltSummarySlack(self._client, self._identity.user_id)
        self.summary_names = people
        if any(c.summary is not None for c in self._policy.channels.values()):
            self.summaries = WeeklySummaries(
                self._store, self._policy, self.summary_slack, attribution
            )
            self._tasks.append(
                asyncio.create_task(self.summaries.run(self._stop), name="jotflow-summaries")
            )
        # Admin-issued, read-only operator tokens (#58), whether or not runs are on.
        self.operators = OperatorTokens(self._store)
        await self.operators.reload()
        self._tasks.append(
            asyncio.create_task(self.operators.run(self._stop), name="jotflow-operators")
        )
        self._server = self._make_server(
            self._settings, GateTokens(self.registry, self.operators), tools
        )
        await self._server.start()
        if self.kinds.on:
            self._runner = self._runner or RunnerClient.from_settings(self._settings)
            for host in self._runner.public_hosts():
                log.warning(
                    "JOTFLOW_RUNNER_ALLOW_PUBLIC is on: runner secrets may go to %s even if it "
                    "resolves to a public address (#234)",
                    host,
                )
            self.kinds = await check_runners(
                self._runner,
                self.kinds,
                self._policy.agent.preset or "",
                attempts=self._info_attempts,
                delay=self._info_delay,
            )
        for reason in self.kinds.off:
            log.warning("%s", reason)
        await self._check_boards()
        if self.repos.owners:
            if self._runner is None:
                log.warning(
                    "jotflow.toml has scopes, but agent runs are off, so no repo is found "
                    "for them; listed repos still work (#236)"
                )
            else:
                self._tasks.append(
                    asyncio.create_task(self.repos.run(self._stop), name="jotflow-repos")
                )
        if self.boards.scoped:
            if self._runner is None:
                log.warning(
                    "jotflow.toml has board scopes, but agent runs are off, so no board is "
                    "found for them; listed boards still work (#309)"
                )
            else:
                self._tasks.append(
                    asyncio.create_task(self.boards.run(self._stop), name="jotflow-boards")
                )
        if self._runner is not None and RunKind.DRAFTING in self.kinds.on:
            await probe_docs(self._runner, self._policy)
        if not self.runs_on:
            return
        runner = self._runner
        assert runner is not None
        self._supervisor = RunSupervisor(
            lifecycle,
            self._store,
            runner,
            poster,
            max_running=self._policy.agent.max_concurrent_runs,
            kinds=self.kinds.on,
        )
        # Only drafting runs come from the debouncer; with them off, queue none.
        if RunKind.DRAFTING in self.kinds.on:
            self._debouncer = Debouncer(self._store, self._policy)
            # After recovery, before the first flush: threads that were waiting
            # out their quiet window when JotFlow stopped (#130).
            restored = await self._debouncer.restore()
            if restored:
                log.info("%d thread(s) still waiting out their quiet window", restored)
        # Questions (#57) only while a runner serves them, as drafting needs the debouncer.
        if RunKind.QUESTION in self.kinds.on:
            self._questions = QuestionHandler(
                self._store,
                self._policy,
                self._question_slack
                or BoltQuestionSlack(self._client, slack_people, BoltHelpSlack(self._client)),
                self._identity.user_id,
            )
        supervise = (
            self._supervisor.run(self._stop)
            if self._tick_seconds is None
            else self._supervisor.run(self._stop, interval=self._tick_seconds)
        )
        self._tasks.append(asyncio.create_task(supervise, name="jotflow-supervisor"))
        if self._debouncer is not None:
            self._tasks.append(
                asyncio.create_task(self._flush_loop(self._debouncer), name="jotflow-debouncer")
            )
        log.info(
            "agent runs are on for %s, up to %d at a time",
            ", ".join(sorted(k.value for k in self.kinds.on)),
            self._policy.agent.max_concurrent_runs,
        )

    async def note(self, decision: Decision, event: Mapping[str, Any] | None = None) -> None:
        """Take one ingest decision: keywords first, then the debouncer (#56).

        The keyword handler runs first, so a person's ``skip:`` or ``re:`` is
        stored before the debouncer would join a reply to its parent's topic.
        The debouncer then leaves a ``skip:`` message, and any reply under a
        ``skip:`` first message, out: no drafting run, and no restarted wait. It
        does nothing while runs are off.

        In a paused channel JotFlow reads nothing, so none of this happens (#28).
        """
        if isinstance(decision, Accepted) and self._store is not None:
            try:
                paused = await self.channel_mode(decision.ref.channel_id) is ChannelMode.PAUSED
            except Exception:
                log.exception("could not read the channel's mode")
                paused = True  # fail closed: read nothing rather than act in a paused channel
            if paused:
                return
        if (
            isinstance(decision, Accepted)
            and decision.kind == "message"
            and event is not None
            and self._questions is not None
        ):
            try:
                await self._questions.on_message(event)
            except Exception:
                log.exception("could not take a question")
            if is_ask(str(event.get("text") or "")):
                # ask: is only a question: no keyword handling, no help, no drafting run,
                # whatever became of it, and even if taking it failed (fail closed).
                # A bot mention goes on to drafting too, which only proposes (#329).
                return
        if isinstance(decision, Accepted) and event is not None and self._keywords is not None:
            try:
                if decision.kind == "message":
                    await self._keywords.on_message(event)
                elif decision.kind == "reaction":
                    await self._keywords.on_reaction(event)
                else:
                    outcome = await self._keywords.on_reaction_removed(event)
                    if outcome.action == "unskipped":
                        # The skip is undone: the thread drafts again (#138).
                        await self._arm_thread(decision.ref.channel_id, outcome.detail)
            except Exception:
                log.exception("could not apply a keyword")
            if decision.kind == "reaction" and self.approval is not None:
                try:
                    await self.approval.on_reaction(event)
                except Exception:
                    log.exception("could not handle a reaction on a card")
            if decision.kind == "message":
                await self._offer_help(decision, event)
        if self._debouncer is None:
            return
        try:
            author = event.get("user") if event is not None else None
            await self._debouncer.note(decision, author if isinstance(author, str) else None)
        except Exception:
            log.exception("could not note a message for the debouncer")

    async def _offer_help(self, decision: Accepted, event: Mapping[str, Any]) -> None:
        """The guide, tip, or nudge, never in a paused channel (#80). Errors are only logged."""
        if self._help is None:
            return
        try:
            if await self.channel_mode(decision.ref.channel_id) is ChannelMode.PAUSED:
                return
            await self._help.on_message(event)
        except Exception:
            log.exception("could not offer help")

    async def channel_mode(self, channel_id: str) -> ChannelMode:
        """The channel's mode, from the store, or propose when nobody set one."""
        setting = await self.store.get_channel_mode(channel_id)
        return ChannelMode.PROPOSE if setting is None else setting.mode

    async def ensure_guides(self, channel_ids: Sequence[str]) -> None:
        """Pin the help guide in each channel that has none yet, at startup (#80)."""
        if self._help is None:
            return
        for channel_id in channel_ids:
            try:
                if await self.channel_mode(channel_id) is not ChannelMode.PAUSED:
                    await self._help.ensure_guide(channel_id)
            except Exception:
                log.exception("could not pin the help guide in %s", channel_id)

    async def _card_loop(self, cards: CardSync) -> None:
        while not self._stop.is_set():
            try:
                if self.auto is not None:
                    await self.auto.approve_due()
                await cards.sync()
                if self.threads is not None:
                    await self.threads.tell()
            except Exception:
                log.exception("card sync failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._card_seconds)

    async def _check_boards(self) -> None:
        """Ask the exec runner whether its write token may update each configured
        board (#309). One it can't is turned off: drafting doesn't see it, and
        propose_issue refuses it. One that can't be checked, such as during a GitHub
        outage or a rate limit, stays on (#311 review): this is a pre-check, and the
        add's own failure, named in the thread, is the real one."""
        boards = {b.board for c in self._policy.channels.values() for b in c.boards}
        if not boards:
            return
        if self._runner is None or RunKind.EXECUTION not in self.kinds.on:
            self.boards.off.update(boards)
            log.warning("boards are off: execution runs are off, so nothing could add to them")
            return
        for board in sorted(boards):
            owner, _, number = board.partition("/")
            try:
                ok = await self._runner.board_access(owner, int(number))
            except RunnerError as exc:
                log.warning(
                    "board %s stays on: its access couldn't be checked at start (%s). If "
                    "adding to it fails, the thread will say so",
                    board,
                    exc,
                )
                continue
            if not ok:
                self.boards.off.add(board)
                log.warning(
                    "board %s is off: the write token can't see or update it; give it "
                    "Projects: Read and write on the organisation (a classic token needs the "
                    "project scope), and check the board's owner and number",
                    board,
                )

    async def _arm_thread(self, channel_id: str, thread_ts: str) -> None:
        """Start a thread's quiet window for a message JotFlow posted for a person."""
        if self._debouncer is not None:
            await self._debouncer.arm(channel_id, thread_ts)

    async def _flush_loop(self, debouncer: Debouncer) -> None:
        while not self._stop.is_set():
            try:
                await debouncer.flush()
            except Exception:
                log.exception("debouncer flush failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._flush_seconds)

    async def stop(self) -> None:
        """Stop in reverse order. Safe to call more than once, or after a failed start."""
        self._stop.set()
        # The supervisor's run() stops live runs on its runners as it exits.
        for task in self._tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        self._tasks = []
        revoked = self.registry.revoke_all()
        if revoked:
            log.info("revoked %d live run token(s) before stopping the MCP server", revoked)
        if self.operators is not None:
            self.operators.forget()  # no operator call lands while the server stops
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop()
        if self.operators is not None:
            with contextlib.suppress(Exception):
                await self.operators.flush()  # last-used for calls since the last reload
        if self._runner is not None:
            await self._runner.aclose()
        if self._store is not None:
            store, self._store = self._store, None
            await store.close()


class _RunnerBoards:
    """The board call, through whichever runner client the service has by then."""

    def __init__(self, service: Orchestrator) -> None:
        self._service = service

    async def add_to_board(
        self, repo: str, issue_number: int, board: str, proposal_id: str
    ) -> None:
        runner = self._service._runner
        if runner is None:
            raise RunnerError("no runner to ask")
        await runner.add_to_board(repo, issue_number, board, proposal_id)
