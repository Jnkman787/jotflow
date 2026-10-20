"""The agent's write tools (#15): its only way to act.

``propose_issue``, ``propose_update``, ``link_message``, and ``ask_channel`` are
for drafting runs (``ask_channel`` also for question runs); ``record_result`` is
for execution runs, per the token table in docs/architecture.md. Every tool:

- takes its channel, thread, run, and proposal from the token's grant, never
  from its arguments, and reports anything outside the grant as not found
- is idempotent: an identical repeat returns the first result and writes
  nothing, so a retry after the server's request timeout cannot duplicate a
  write. The write and its ledger entry run in a cancellation-shielded scope,
  so the timeout cannot cut them apart.
- is capped per run, and records what it did in the ledger before it returns

Refusals are :class:`~jotflow.whiteboard.tools.ToolError`, which the model sees
as a tool result. Reasons are written here, never quoting Slack text.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar
from urllib.parse import unquote

import anyio
from pydantic import BaseModel, Field, model_validator

from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    RelatedIssue,
    RunKind,
    RunStatus,
    UpdateProposal,
)
from jotflow.policy import Policy, PolicyViolation, label_problem
from jotflow.repo_catalog import RepoCatalog
from jotflow.store import ConflictError, NotFoundError, Store, ToolCallRecord, TransitionError
from jotflow.store.base import check_github_login, utc_now
from jotflow.title_pattern import Match, match_title_async
from jotflow.whiteboard.doc_tools import PASSAGES, DocReads, passages_row
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError, ToolSpec

log = logging.getLogger("jotflow.whiteboard")

MAX_PROPOSALS_PER_RUN = 3
# A run may propose a title its repo's title_pattern refuses this many times (#216).
MAX_TITLE_REFUSALS = 3
TITLE_REFUSED = (
    "this repo's titles must match title_pattern {pattern}; propose again with a title that matches"
)
TOO_MANY_TITLES = (
    "this run's titles were refused {n} times; stop proposing and leave the title to a person"
)
MAX_QUESTIONS_PER_RUN = 2
MAX_LINKS_PER_RUN = 200
QUESTION_INTERVAL = timedelta(seconds=30)
MAX_QUESTION_LENGTH = 500
MAX_ANSWER_LENGTH = 500
# Every answer says whose it is, so nobody mistakes it for a person or a JotFlow fact.
ANSWER_PREFIX = "Answer from the agent:"
MAX_PARTICIPANTS = 50

DRAFTING = frozenset({RunKind.DRAFTING})
ASKING = frozenset({RunKind.DRAFTING, RunKind.QUESTION})
ANSWERING = frozenset({RunKind.QUESTION})
EXECUTION = frozenset({RunKind.EXECUTION})

PROPOSE_TOOLS = ("propose_issue", "propose_update")
# A result lands while the write is in progress: executing, or reconciling when a
# retry after an interrupted run finds the marker it left (or gives up).
RESULT_STATES = frozenset({ProposalState.EXECUTING, ProposalState.RECONCILING})

_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")
# <!channel>, <!here>, <!everyone>, <!subteam^S123|@team>, <!date...>: any special mention.
_SPECIAL_MENTION = re.compile(r"<![^>]*>")
_BARE_BROADCAST = re.compile(r"(?<![\w@])@(?:channel|here|everyone)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ThreadPoster(Protocol):
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        """Post ``text`` as a reply in the thread and return the new message's ts.

        Raises :class:`PostError` if Slack refuses it.
        """
        ...

    async def post_plain(self, channel_id: str, thread_ts: str, text: str) -> str:
        """Post ``text`` as a plain_text block reply, which Slack never formats (#57).

        Raises :class:`PostError` if Slack refuses it.
        """
        ...


class PostError(Exception):
    """Slack would not post the reply. The message names Slack's error code only."""


# Arguments the agent sends.


def _check_participants(value: list[str]) -> list[str]:
    if len(value) > MAX_PARTICIPANTS:
        raise ValueError(f"at most {MAX_PARTICIPANTS} participants")
    for user in value:
        if not _SLACK_USER.fullmatch(user):
            raise ValueError("participants are Slack user ids such as U0123ABCD")
    return sorted(set(value))


class ProposeIssueArgs(IssueProposal):
    participants: list[str] = Field(
        default_factory=list,
        description="Slack user ids of the poster and anyone mentioned. Informational: "
        "they are never made assignees.",
    )

    @model_validator(mode="after")
    def _participants(self) -> ProposeIssueArgs:
        self.participants = _check_participants(self.participants)
        return self


class ProposeUpdateArgs(UpdateProposal):
    participants: list[str] = Field(
        default_factory=list,
        description="Slack user ids of the poster and anyone mentioned. Informational: "
        "they are never made assignees.",
    )

    @model_validator(mode="after")
    def _participants(self) -> ProposeUpdateArgs:
        self.participants = _check_participants(self.participants)
        return self


class LinkMessageArgs(BaseModel):
    ts: str = Field(description="The message's Slack timestamp, as get_work served it")
    decision: Literal["new_topic", "existing_topic", "chatter"]
    topic_id: str | None = Field(default=None, description="Required for existing_topic")
    title: str | None = Field(
        default=None, max_length=200, description="Required for new_topic: a short title"
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _shape(self) -> LinkMessageArgs:
        if self.decision == "new_topic" and (not self.title or self.topic_id):
            raise ValueError("new_topic needs a title and no topic_id")
        if self.decision == "existing_topic" and (not self.topic_id or self.title):
            raise ValueError("existing_topic needs a topic_id and no title")
        if self.decision == "chatter" and (self.topic_id or self.title):
            raise ValueError("chatter takes no topic_id or title")
        return self


class AskChannelArgs(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)


class AnswerArgs(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_ANSWER_LENGTH)


class RecordResultArgs(ExecutionResult):
    pass


# Results. Ids and states only, so the ledger never holds message text.


class ProposeResult(BaseModel):
    proposal_id: str
    state: ProposalState
    superseded: list[str]
    dropped_related: int = Field(
        default=0,
        description="Related issues left off the card: not in this channel's repos, repeated, "
        "or the issue an update is for",
    )


class LinkResult(BaseModel):
    ts: str
    topic_id: str | None
    chatter: bool
    decided_by: Literal["agent", "person"]


class AskResult(BaseModel):
    posted_ts: str


class AnswerResult(BaseModel):
    posted_ts: str
    removed: int = Field(
        default=0, description="Links, addresses and other repos' references JotFlow removed"
    )


class RecordOutcome(BaseModel):
    proposal_id: str
    state: ProposalState


R = TypeVar("R", bound=BaseModel)


def args_hash(tool: str, arguments: BaseModel) -> str:
    """The ledger key for a call: the tool and its canonical arguments."""
    canonical = json.dumps(
        {"tool": tool, "arguments": arguments.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# A Slack workspace or message link, in any form an agent might write one: a URL
# on any slack.com host (workspaces, app., files., enterprise grids), Slack's own
# <https://...|label> markup (caught by the URL), or a slack:// app link.
_SLACK_LINK = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)*slack\.com\b|\bslack://")
NO_SLACK_LINKS = (
    "This channel keeps Slack out of GitHub (issue_links = none): remove every Slack link "
    "from the title, body and comment, and don't name people from the thread."
)


# A GitHub @-mention as GitHub reads one (#250): "@" not preceded by a word
# character, so an email address doesn't count, then a login (1-39 letters,
# digits or single hyphens), or a team as @org/team. Code is never a mention.
_MENTION = re.compile(
    r"(?<![A-Za-z0-9_`])@([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})(/[A-Za-z0-9_.-]+)?"
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)", re.M | re.S)
_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)


def mentions(text: str) -> list[str]:
    """Every @login or @org/team GitHub would ping in ``text``, outside code (#250).

    Read four ways: as written, with invisible format characters removed, and
    each of those with HTML entities decoded once, as GitHub does: ``&#64;login``
    and ``&commat;login`` ping. A zero-width space or an entity can't hide a
    mention, or make one login look like another: every reading must pass.
    """
    visible = "".join(c for c in text if unicodedata.category(c) != "Cf")
    readings = (text, visible, html.unescape(text), html.unescape(visible))
    found: list[str] = []
    for reading in dict.fromkeys(readings):
        prose = _CODE_SPAN.sub(" ", _FENCE.sub("\n", reading))
        found += [m.group(1) + (m.group(2) or "") for m in _MENTION.finditer(prose)]
    return list(dict.fromkeys(found))


def _not_linked(login: str) -> str:
    return (
        f"@{login} would ping someone on GitHub, and it isn't the linked GitHub login of a "
        "participant. Write the name without @, or mention only a participant who linked "
        "their login."
    )


def _for_link_check(text: str) -> str:
    """``text`` as GitHub and a browser would read a link in it: percent-escapes
    decoded (repeatedly, so ``%252E`` counts), compatibility forms folded (a
    full-width dot is a dot), and invisible control and format characters such
    as zero-width spaces removed, so none of them can hide a Slack domain."""
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) not in ("Cc", "Cf"))


# Anything Slack, a browser or a mail client could turn into a link (#57): a URL
# with a scheme, a www. name, an email address, or a bare domain such as
# example.com/x, which Slack auto-links in a message's text.
_URLISH = re.compile(
    r"(?i)(?:"
    r"\b[a-z][a-z0-9+.-]*://[^\s<>|]*"
    r"|\b(?:mailto|slack|tel|data|javascript):[^\s<>|]*"
    r"|\bwww\.[^\s<>|]*"
    r"|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    # Labels and the top-level domain in any script, so an IDN such as
    # \u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444 counts.
    r"|\b(?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+[^\W\d_]{2,63}\b(?:/[^\s<>|]*)?"
    r")"
)
# An issue reference, owner/name#42.
_REPO_REF = re.compile(r"(?<![\w/.-])([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)#([0-9]+)\b")
_HELD = re.compile("\x00([0-9]+)\x00")
LINK_REMOVED = "[link removed]"


def safe_text(text: str, repos: Collection[str]) -> tuple[str, int]:
    """``text`` as JotFlow posts it in a thread, and how much was removed (#57).

    No link of any kind survives, after the same fold the Slack-link check uses,
    so a zero-width space or a full-width dot can't hide one. An issue is named
    only as ``owner/name#N`` text, and only for ``repos``. Any other repo's
    reference is dropped. @-mentions lose their @, so they can't ping on
    GitHub. Then :func:`clean_question` escapes Slack's markup.
    """
    allowed = {repo.casefold(): repo for repo in repos}
    # Slack's own broadcasts first, while their markup is whole, and again until
    # nothing changes, so a removal can't join the pieces of another.
    folded = _for_link_check(text)
    while True:
        stripped = _BARE_BROADCAST.sub("", _SPECIAL_MENTION.sub("", folded))
        if stripped == folded:
            break
        folded = stripped
    held: list[str] = []
    removed = 0

    def hold(match: re.Match[str]) -> str:
        nonlocal removed
        repo = allowed.get(match.group(1).casefold())
        if repo is None:
            removed += 1
            return ""
        held.append(f"{repo}#{match.group(2)}")
        return f"\x00{len(held) - 1}\x00"

    def link(match: re.Match[str]) -> str:
        nonlocal removed
        removed += 1
        return LINK_REMOVED

    out = _REPO_REF.sub(hold, folded)
    # @ goes before links are removed, so taking it away can't leave a domain behind.
    out = _MENTION.sub(lambda m: m.group(1) + (m.group(2) or ""), out)
    out = _URLISH.sub(link, out)
    out = _HELD.sub(lambda m: held[int(m.group(1))], out)
    out = re.sub(r"[ \t]{2,}", " ", out)  # what a removal leaves behind
    return clean_question(out), removed


def clean_question(text: str) -> str:
    """``text`` made safe to post: no broadcasts, no Slack markup, no control characters."""
    cleaned = _SPECIAL_MENTION.sub("", text)
    cleaned = _BARE_BROADCAST.sub("", cleaned)
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cleaned = cleaned.strip()
    if not cleaned:
        raise ToolError("the question is empty once broadcast mentions are removed")
    return cleaned


def _check_removals(labels: list[str], assignees: list[str]) -> None:
    """Removals need no allowlist, but they still reach the card and GitHub."""
    for label in labels:
        problem = label_problem(label)
        if problem is not None:
            raise ToolError(f"a label to remove {problem}")
    for login in assignees:
        try:
            check_github_login(login)
        except ValueError:
            raise ToolError("an assignee to remove is not a GitHub login") from None


class _RunLocks:
    """One lock per run, so a run's calls are handled one at a time."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, run_id: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(run_id, (asyncio.Lock(), 0))
        self._locks[run_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[run_id]
            if users == 1:
                del self._locks[run_id]
            else:
                self._locks[run_id] = (lock, users - 1)


PAUSED_REASON = "this channel is paused; JotFlow writes nothing there until it is unpaused"
# record_result only records a GitHub write the agent already made, approved before
# the pause; refusing it would strand the proposal, so it is allowed (#28).
WRITES_WHILE_PAUSED = frozenset({"record_result"})


class WriteTools:
    """The handlers, with what they need. Build the specs with :func:`write_tools`."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        poster: ThreadPoster,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], str] = lambda: secrets.token_hex(8),
        repos: RepoCatalog | None = None,
        docs: DocReads | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        # The run's doc reads, for the card's long-passage flag (#209).
        self._docs = docs
        self._poster = poster
        self._clock = clock
        self._new_id = new_id
        # The channel's repos, listed and found by scopes: the write allowlist (#236).
        self._repos = repos if repos is not None else RepoCatalog.static(policy)
        self._locks = _RunLocks()
        self._title_refusals: dict[str, int] = {}
        # Matches that ran out of time, by repo: a sign the pattern itself is broken.
        self.pattern_timeouts: dict[str, int] = {}

    async def _check_title(self, grant: Grant, repo: str, title: str) -> None:
        """Refuse a title the repo's title_pattern doesn't fully match (#216).

        Updates never change a title, so only propose_issue calls this.
        """
        refused = self._title_refusals.get(grant.run_id, 0)
        if refused >= MAX_TITLE_REFUSALS:
            raise ToolError(TOO_MANY_TITLES.format(n=refused))
        pattern = self._repos.repo(grant.channel_id, repo).title_pattern
        if pattern is None:
            return
        result = await match_title_async(pattern, title)
        if result is Match.MATCH:
            return
        if result is Match.TIMEOUT:
            # Fail closed: a title that can't be checked in time is refused. Never log it.
            self.pattern_timeouts[repo] = self.pattern_timeouts.get(repo, 0) + 1
            log.warning(
                "title_pattern for %s ran out of time; refused the title (%d so far)",
                repo,
                self.pattern_timeouts[repo],
            )
        await self._forget_ended_runs()
        self._title_refusals[grant.run_id] = refused + 1
        raise ToolError(TITLE_REFUSED.format(pattern=pattern))

    async def _forget_ended_runs(self) -> None:
        """Drop refusal counts for runs that have ended. Only runs with one are kept."""
        for run_id in list(self._title_refusals):
            run = await self._store.get_run(run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                del self._title_refusals[run_id]

    async def _once(
        self,
        grant: Grant,
        tool: str,
        arguments: BaseModel,
        result_type: type[R],
        act: Callable[[], Awaitable[R]],
    ) -> R:
        key = args_hash(tool, arguments)
        async with self._locks.hold(grant.run_id):
            await self._check_running(grant)
            earlier = await self._store.find_tool_call(grant.run_id, tool, key)
            if earlier is not None:
                return result_type.model_validate_json(earlier)
            if tool not in WRITES_WHILE_PAUSED:
                await self._check_not_paused(grant)
            with anyio.CancelScope(shield=True):
                result = await act()
                await self._store.record_tool_call(
                    ToolCallRecord(
                        run_id=grant.run_id,
                        tool=tool,
                        args_hash=key,
                        channel_id=grant.channel_id,
                        thread_ts=grant.thread_ts,
                        result_json=result.model_dump_json(),
                        called_at=self._clock(),
                    )
                )
            return result

    async def _check_not_paused(self, grant: Grant) -> None:
        """A paused channel writes nothing, even from a run that started before (#28)."""
        setting = await self._store.get_channel_mode(grant.channel_id)
        if setting is not None and setting.mode is ChannelMode.PAUSED:
            raise ToolError(PAUSED_REASON)

    async def _check_running(self, grant: Grant) -> None:
        # The token is revoked when a run ends; this is the second guard, in
        # case a revoke is ever missed. The deadline is not checked: the token's
        # short grace lets a run's last call land, such as the result of a
        # write that already happened, while the overdue sweep times it out.
        run = await self._store.get_run(grant.run_id)
        if (
            run is None
            or run.status is not RunStatus.RUNNING
            or (run.kind, run.channel_id) != (grant.kind, grant.channel_id)
            # The same matching get_work does: the run's own thread, or for an
            # execution run its own proposal.
            or (run.kind is not RunKind.EXECUTION and run.thread_ts != grant.thread_ts)
            or (run.kind is RunKind.EXECUTION and run.proposal_id != grant.proposal_id)
        ):
            raise ToolError(f"run {grant.run_id} is not running")

    async def _cap(self, grant: Grant, tools: tuple[str, ...], limit: int, what: str) -> None:
        if await self._store.count_tool_calls(grant.run_id, tools) >= limit:
            raise ToolError(f"this run has used its {limit} {what}")

    async def _packet_ref(self, grant: Grant, ts: str) -> MessageRef:
        ref = await self._store.packet_ref(grant.run_id, grant.channel_id, ts)
        if ref is None:
            raise ToolError(f"message {ts} is not in this run's packet")
        return ref

    async def _channel_topic_issue(
        self, grant: Grant, topic_id: str
    ) -> tuple[str | None, int | None]:
        record = await self._store.get_topic(topic_id)
        if record is None or record.topic.channel_id != grant.channel_id:
            raise ToolError(f"no topic {topic_id}")
        return record.topic.repo, record.topic.issue_number

    async def _linked_logins(self, participants: list[str]) -> set[str]:
        logins: set[str] = set()
        for user in participants:
            link = await self._store.get_identity(user)
            if link is not None:
                logins.add(link.github_login.casefold())
        return logins

    async def _check_assignees(self, assignees: list[str], participants: list[str]) -> None:
        if not assignees:
            return
        logins = await self._linked_logins(participants)
        for login in assignees:
            if login.casefold() not in logins:
                raise ToolError(
                    f"assignee {login} is not the linked GitHub login of a participant; "
                    "assign only someone who said they will take it"
                )

    async def _check_mentions(self, participants: list[str], *texts: str | None) -> None:
        """Only a linked participant may be @-mentioned in what goes to GitHub (#250)."""
        found = [m for text in texts if text for m in mentions(text)]
        if not found:
            return
        logins = await self._linked_logins(participants)
        for login in found:
            if "/" in login or login.casefold() not in logins:
                raise ToolError(_not_linked(login))

    def _check_no_slack_links(self, *texts: str | None) -> None:
        """With ``issue_links = "none"``, nothing Slack-shaped goes to GitHub (#168, #32)."""
        if self._policy.issue_links != "none":
            return
        for text in texts:
            if text and _SLACK_LINK.search(_for_link_check(text)):
                raise ToolError(NO_SLACK_LINKS)

    def _related(
        self, grant: Grant, related: list[RelatedIssue], *, own: tuple[str, int] | None = None
    ) -> tuple[list[RelatedIssue], int]:
        """The related issues the card may show (#36): this channel's repos only, each once.

        Each repo is written as jotflow.toml spells it. Anything else is dropped, not
        refused, so a stray search hit doesn't cost the whole draft, and the count
        goes back to the agent.
        """
        kept: list[RelatedIssue] = []
        seen: set[tuple[str, int]] = set()
        if own is not None:
            seen.add((own[0].casefold(), own[1]))
        for entry in related:
            try:
                repo = self._repos.repo(grant.channel_id, entry.repo).repo
            except PolicyViolation:
                continue
            key = (repo.casefold(), entry.number)
            if key in seen:
                continue
            seen.add(key)
            kept.append(entry.model_copy(update={"repo": repo}))
        return kept, len(related) - len(kept)

    def _repo_and_labels(self, grant: Grant, repo: str, labels: list[str]) -> tuple[str, list[str]]:
        try:
            policy = self._repos.repo(grant.channel_id, repo)
            return policy.repo, policy.check_labels(labels)
        except PolicyViolation as refused:
            raise ToolError(str(refused)) from None

    async def _sources(self, grant: Grant, sources: list[MessageRef]) -> list[MessageRef]:
        return [await self._packet_ref(grant, source.ts) for source in sources]

    async def _add(
        self, grant: Grant, proposal: IssueProposal | UpdateProposal, participants: list[str]
    ) -> ProposeResult:
        await self._cap(grant, PROPOSE_TOOLS, MAX_PROPOSALS_PER_RUN, "proposals")
        thread = grant.thread_ts
        if thread is None:  # pragma: no cover - a drafting grant always names its thread
            raise ToolError("this run has no thread")
        record = ProposalRecord(
            id=self._new_id(),
            channel_id=grant.channel_id,
            thread_ts=thread,
            run_id=grant.run_id,
            proposal=proposal,
            participants=participants,
            created_at=self._clock(),
        )
        try:
            stored = await self._store.add_proposal(record)
        except (NotFoundError, ConflictError):
            raise ToolError(f"no topic {proposal.topic_id}") from None
        superseded = await self._store.supersede_pending(
            grant.channel_id, thread, keep_id=stored.id
        )
        await self._flag_passages(grant, stored.id, proposal)
        return ProposeResult(proposal_id=stored.id, state=stored.state, superseded=superseded)

    async def _flag_passages(
        self, grant: Grant, proposal_id: str, proposal: IssueProposal | UpdateProposal
    ) -> None:
        """Note on the ledger which docs the proposal copies at length, for its card (#209)."""
        if self._docs is None:
            return
        if isinstance(proposal, IssueProposal):
            texts: list[str | None] = [proposal.title, proposal.body]
        else:
            texts = [proposal.changes.body, proposal.changes.comment]
        copied = self._docs.copied_from(grant.run_id, texts)
        if not copied:
            return
        await self._store.record_tool_call(
            ToolCallRecord(
                run_id=grant.run_id,
                tool=PASSAGES,
                args_hash=f"proposal:{proposal_id}",
                channel_id=grant.channel_id,
                thread_ts=grant.thread_ts,
                result_json=passages_row(proposal_id, copied),
                called_at=self._clock(),
            )
        )

    # The tools.

    async def propose_issue(self, grant: Grant, arguments: Any) -> BaseModel:
        args: ProposeIssueArgs = arguments

        async def act() -> ProposeResult:
            if args.topic_id is None:
                raise ToolError("name the topic: start one with link_message, then use its id")
            linked_repo, linked_issue = await self._channel_topic_issue(grant, args.topic_id)
            if linked_issue is not None:
                raise ToolError(
                    f"topic {args.topic_id} already has issue {linked_repo}#{linked_issue}; "
                    "use propose_update"
                )
            repo, labels = self._repo_and_labels(grant, args.repo, args.labels)
            await self._check_title(grant, repo, args.title)
            self._check_no_slack_links(args.title, args.body)
            await self._check_mentions(args.participants, args.title, args.body)
            await self._check_assignees(args.assignees, args.participants)
            related, dropped = self._related(grant, args.related)
            draft = IssueProposal.model_validate(
                {
                    **args.model_dump(exclude={"participants"}),
                    "repo": repo,
                    "labels": labels,
                    "sources": await self._sources(grant, args.sources),
                    "related": [r.model_dump() for r in related],
                }
            )
            result = await self._add(grant, draft, args.participants)
            return result.model_copy(update={"dropped_related": dropped})

        return await self._once(grant, "propose_issue", args, ProposeResult, act)

    async def propose_update(self, grant: Grant, arguments: Any) -> BaseModel:
        args: ProposeUpdateArgs = arguments

        async def act() -> ProposeResult:
            repo_of_topic, issue_of_topic = await self._channel_topic_issue(grant, args.topic_id)
            repo, labels = self._repo_and_labels(grant, args.repo, args.changes.add_labels)
            self._check_no_slack_links(args.changes.body, args.changes.comment)
            if (
                repo_of_topic is None
                or repo_of_topic.casefold() != repo.casefold()
                or issue_of_topic != args.issue_number
            ):
                raise ToolError(
                    f"topic {args.topic_id} is not linked to {repo}#{args.issue_number}"
                )
            await self._check_mentions(args.participants, args.changes.body, args.changes.comment)
            await self._check_assignees(args.changes.add_assignees, args.participants)
            _check_removals(args.changes.remove_labels, args.changes.remove_assignees)
            changes = args.changes.model_copy(update={"add_labels": labels})
            related, dropped = self._related(grant, args.related, own=(repo, args.issue_number))
            draft = UpdateProposal.model_validate(
                {
                    **args.model_dump(exclude={"participants"}),
                    "repo": repo,
                    "changes": changes.model_dump(),
                    "sources": await self._sources(grant, args.sources),
                    "related": [r.model_dump() for r in related],
                }
            )
            result = await self._add(grant, draft, args.participants)
            return result.model_copy(update={"dropped_related": dropped})

        return await self._once(grant, "propose_update", args, ProposeResult, act)

    async def link_message(self, grant: Grant, arguments: Any) -> BaseModel:
        args: LinkMessageArgs = arguments

        async def act() -> LinkResult:
            await self._cap(grant, ("link_message",), MAX_LINKS_PER_RUN, "message links")
            ref = await self._packet_ref(grant, args.ts)
            existing = await self._store.get_message(ref.channel_id, ref.ts)
            if existing is not None and existing.linked_by == "person":
                link = existing
            elif args.decision == "chatter":
                link = await self._store.mark_chatter(ref)
            else:
                if args.decision == "new_topic":
                    try:
                        topic = await self._store.create_topic(grant.channel_id, args.title or "")
                    except ValueError:
                        raise ToolError("that title has no words left once cleaned") from None
                    # Two store writes. Both run in the shielded block, so a timeout
                    # cannot split them; only a store failure in between could leave
                    # an empty topic, which nothing links to and which then expires.
                    topic_id = topic.topic.id
                else:
                    topic_id = args.topic_id or ""
                    await self._channel_topic_issue(grant, topic_id)
                link = await self._store.link_message(ref, topic_id)
            return LinkResult(
                ts=link.ref.ts,
                topic_id=link.topic_id,
                chatter=link.chatter,
                decided_by=link.linked_by,
            )

        return await self._once(grant, "link_message", args, LinkResult, act)

    async def ask_channel(self, grant: Grant, arguments: Any) -> BaseModel:
        args: AskChannelArgs = arguments
        thread = grant.thread_ts
        if thread is None:  # pragma: no cover - drafting and question grants always name one
            raise ToolError("this run has no thread to ask in")

        async def act() -> AskResult:
            await self._cap(grant, ("ask_channel",), MAX_QUESTIONS_PER_RUN, "questions")
            since = self._clock() - QUESTION_INTERVAL
            if await self._store.count_thread_calls(
                grant.channel_id, thread, "ask_channel", since=since
            ):
                raise ToolError(
                    f"a question was asked in this thread in the last "
                    f"{int(QUESTION_INTERVAL.total_seconds())} seconds; wait for an answer"
                )
            # The same rewrite as an answer: it posts in the same place (#57).
            text, _ = safe_text(args.text, self._channel_repos(grant))
            try:
                posted = await self._poster.post(grant.channel_id, thread, text)
            except PostError as refused:
                raise ToolError(f"Slack did not post the question ({refused})") from None
            return AskResult(posted_ts=posted)

        return await self._once(grant, "ask_channel", args, AskResult, act)

    async def answer(self, grant: Grant, arguments: Any) -> BaseModel:
        """A question run's one answer, in its thread, as plain text (#57)."""
        args: AnswerArgs = arguments
        thread = grant.thread_ts
        if thread is None:  # pragma: no cover - question grants always name one
            raise ToolError("this run has no thread to answer in")

        async def act() -> AnswerResult:
            await self._cap(grant, ("answer",), 1, "answers")
            text, removed = safe_text(args.text, self._channel_repos(grant))
            try:
                posted = await self._poster.post_plain(
                    grant.channel_id, thread, f"{ANSWER_PREFIX} {text}"
                )
            except PostError as refused:
                raise ToolError(f"Slack did not post the answer ({refused})") from None
            return AnswerResult(posted_ts=posted, removed=removed)

        return await self._once(grant, "answer", args, AnswerResult, act)

    def _channel_repos(self, grant: Grant) -> list[str]:
        """The repos an answer may name: listed and found by scope, as propose_issue resolves."""
        return [entry.repo for entry in self._repos.repos(grant.channel_id)]

    async def record_result(self, grant: Grant, arguments: Any) -> BaseModel:
        args: RecordResultArgs = arguments
        proposal_id = grant.proposal_id or ""

        async def act() -> RecordOutcome:
            record = await self._store.get_proposal(proposal_id)
            if record is None or record.channel_id != grant.channel_id:
                raise ToolError(f"no proposal {proposal_id}")
            result = ExecutionResult.model_validate(args.model_dump())
            to = ProposalState.APPLIED if result.issue_url else ProposalState.FAILED
            try:
                updated = await self._store.transition(
                    proposal_id, to, expected=RESULT_STATES, result=result
                )
            except TransitionError:
                raise ToolError(
                    f"proposal {proposal_id} is {record.state.value}, "
                    "not executing or reconciling; a result is recorded once"
                ) from None
            except ValueError:
                raise ToolError(
                    "the issue URL does not match the approved repo, or the approved issue"
                ) from None
            await self._link_topic(updated, result)
            return RecordOutcome(proposal_id=updated.id, state=updated.state)

        return await self._once(grant, "record_result", args, RecordOutcome, act)

    async def _link_topic(self, record: ProposalRecord, result: ExecutionResult) -> None:
        proposal = record.proposal
        ref = result.issue_ref()
        if record.state is not ProposalState.APPLIED or ref is None:
            return
        if not isinstance(proposal, IssueProposal) or proposal.topic_id is None:
            return
        topic = await self._store.get_topic(proposal.topic_id)
        if topic is not None and topic.topic.issue_number is None:
            await self._store.set_topic_issue(proposal.topic_id, proposal.repo, ref[1])


_DESCRIPTIONS = {
    "propose_issue": (
        "Propose a new GitHub issue for a topic in this run's channel. People approve it "
        "on a Slack card before anything is written. Name the topic with topic_id; start "
        "one with link_message first. Use only the channel's repos and allowed labels. "
        "Assign only someone in participants who said they will take it. @-mention only a "
        "participant who linked their GitHub login; write anyone else's name without @."
    ),
    "propose_update": (
        "Propose a change to the issue a topic is already linked to: a body edit, a "
        "comment, or label or assignee changes. People approve it before it is written. "
        "@-mention only a participant who linked their GitHub login."
    ),
    "link_message": (
        "Decide where a message from get_work belongs: a new topic, an existing topic, or "
        "chatter. A person's earlier decision always stands."
    ),
    "ask_channel": (
        "Ask one short clarifying question in this run's own Slack thread, when you are "
        "unsure. Broadcast mentions, links and @-mentions are removed. Asked at most twice "
        "per run."
    ),
    "answer": (
        "Answer the question in this run's own Slack thread, once, in at most 3 short "
        "sentences. Name issues as owner/name#42 from this channel's repos only. Links, "
        "web and email addresses, other repos and @-mentions are removed. JotFlow posts "
        "it as plain text, labelled as the agent's answer."
    ),
    "record_result": (
        "Report the outcome of this execution run: the URL of the issue or comment you "
        "wrote, or the error that stopped you. Call it once."
    ),
}


def write_tools(
    store: Store,
    policy: Policy,
    poster: ThreadPoster,
    *,
    clock: Callable[[], datetime] = utc_now,
    new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    repos: RepoCatalog | None = None,
    docs: DocReads | None = None,
) -> tuple[ToolSpec, ...]:
    """The write tools, bound to their dependencies. Built once at startup."""
    tools = WriteTools(store, policy, poster, clock=clock, new_id=new_id, repos=repos, docs=docs)
    plan: tuple[tuple[str, type[BaseModel], frozenset[RunKind]], ...] = (
        ("propose_issue", ProposeIssueArgs, DRAFTING),
        ("propose_update", ProposeUpdateArgs, DRAFTING),
        ("link_message", LinkMessageArgs, DRAFTING),
        ("ask_channel", AskChannelArgs, DRAFTING),
        ("answer", AnswerArgs, ANSWERING),
        ("record_result", RecordResultArgs, EXECUTION),
    )
    return tuple(
        ToolSpec(
            name=name,
            description=_DESCRIPTIONS[name],
            input_model=model,
            handler=getattr(tools, name),
            kinds=kinds,
        )
        for name, model, kinds in plan
    )
