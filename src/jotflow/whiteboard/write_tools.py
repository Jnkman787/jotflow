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
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, TypeVar
from urllib.parse import unquote

import anyio
from pydantic import BaseModel, Field, model_validator

from jotflow.board_catalog import BoardCatalog
from jotflow.models import (
    MAX_ATTACHED,
    MAX_RELATED,
    SLACK_FILE_ID,
    AssigneeReason,
    AttachedFile,
    ChannelMode,
    ExecutionResult,
    IssueChanges,
    IssueProposal,
    MessageRef,
    PendingDraft,
    ProposalRecord,
    ProposalState,
    ProposedBoard,
    RelatedIssue,
    RunKind,
    RunStatus,
    UpdateProposal,
    check_reasons,
)
from jotflow.name_match import PACKET_PEOPLE, Names, candidates, only
from jotflow.orchestrator.slack_files import (
    SERVED_FILE,
    FileSlack,
    attachments_section,
    read_text,
)
from jotflow.orchestrator.slack_files import current as current_file
from jotflow.policy import Policy, PolicyViolation, label_problem
from jotflow.repo_catalog import RepoCatalog
from jotflow.store import ConflictError, NotFoundError, Store, ToolCallRecord, TransitionError
from jotflow.store.base import StaleRevisionError, check_github_login, utc_now
from jotflow.text import without_unsafe_chars
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
# A question run changes nothing, so its answer may never say it did (#329). First
# person only: "#42 was created last week" is fine, "I've filed it" is not.
_CLAIM = re.compile(
    r"\b(?:i|we|jotflow)(?:['\u2019]ve| have| has)?\s+(?:just\s+|now\s+|already\s+)?"
    r"(?:created|filed|opened|assigned|added|updated|changed|edited|closed|made|moved)\b",
    re.IGNORECASE,
)
CLAIMS_A_CHANGE = (
    "an answer can't say a change was made: this run changes nothing, and any change "
    "comes as a card for a person to approve. Answer only what was asked."
)


def claims_a_change(text: str) -> bool:
    """Whether ``text`` says, in the first person, that something was changed (#329).

    Read as written and folded: invisible format characters such as a zero-width
    space removed, then compatibility forms folded, so neither hides a claim.
    """
    visible = "".join(c for c in text if unicodedata.category(c) != "Cf")
    folded = unicodedata.normalize("NFKC", visible)
    return any(_CLAIM.search(reading) for reading in (text, folded))


MAX_PARTICIPANTS = 50

DRAFTING = frozenset({RunKind.DRAFTING})
ASKING = frozenset({RunKind.DRAFTING, RunKind.QUESTION})
ANSWERING = frozenset({RunKind.QUESTION})
EXECUTION = frozenset({RunKind.EXECUTION})

PROPOSE_TOOLS = ("propose_issue", "propose_update")
MAX_BOARD_REASON = 200
# Revisions have their own cap, so a busy run can still propose for other topics (#304).
MAX_REVISIONS_PER_RUN = 3
# Who wrote some Slack messages, as the approval gate reads them.
Authors = Callable[[Sequence[MessageRef]], Awaitable[set[str]]]
# The people a message @-mentions, from Slack (#305).
Mentions = Callable[[MessageRef], Awaitable[set[str]]]
# A message's text, and a person's Slack names, for named_in_text (#331).
Texts = Callable[[MessageRef], Awaitable[str]]
NamesOf = Callable[[str], Awaitable[Names]]
# A result lands while the write is in progress: executing, or reconciling when a
# retry after an interrupted run finds the marker it left (or gives up).
RESULT_STATES = frozenset({ProposalState.EXECUTING, ProposalState.RECONCILING})

_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")
# <!channel>, <!here>, <!everyone>, <!subteam^S123|@team>, <!date...>: any special mention.
_SPECIAL_MENTION = re.compile(r"<![^>]*>")
_BARE_BROADCAST = re.compile(r"(?<![\w@])@(?:channel|here|everyone)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# The reply to a message about JotFlow itself (#336): JotFlow's own words, never the agent's.
POINT_TO_HELP = (
    "This sounds like it's about JotFlow itself, so it isn't drafted as an issue. "
    "To ask JotFlow something, start a message with ask: or mention it. "
    "/jotflow help shows what it can do."
)
_EVER = datetime(2000, 1, 1, tzinfo=UTC)


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


# Attachments (#333): fixed reasons, never quoting the file.
ATTACH_OFF = "this channel's attachments setting doesn't allow copying file text into issues"
ATTACH_NOT_SERVED = "isn't a file this run was given the text of"
ATTACH_GONE = "is no longer shared in this channel"
ATTACH_CHANGED = "changed since this run read it; draft from the thread again"
ATTACH_SLACK_LINK = "holds a Slack link, and this channel sends none to GitHub"
ATTACH_TOO_LONG = "the body with its attachments would pass GitHub's length limit; attach fewer"
# GitHub's issue body limit is 65,536 characters; JotFlow's footer needs the rest.
MAX_BODY_WITH_FILES = 60_000


def _check_body_with_files(body: str, files: Sequence[AttachedFile]) -> None:
    """The issue body with its Attachments section stays inside GitHub's limit (#346
    follow-up): checked when files are attached, and again when a revision changes
    the body they go with."""
    if not files:
        return
    section = attachments_section([(a.name, a.text) for a in files])
    if len(body) + len(section) > MAX_BODY_WITH_FILES:
        raise ToolError(ATTACH_TOO_LONG)


class ProposeIssueArgs(IssueProposal):
    participants: list[str] = Field(
        default_factory=list,
        description="Slack user ids of the poster and anyone mentioned. Informational: "
        "they are never made assignees.",
    )
    attachments: list[Annotated[str, Field(pattern=SLACK_FILE_ID)]] = Field(
        default_factory=list,
        max_length=MAX_ATTACHED,
        description=(
            "Optional. Up to 3 `file_id`s of files in this thread whose `text` the packet "
            "gave. JotFlow copies each file's text into the issue, as a code block (#333)"
        ),
    )

    @model_validator(mode="after")
    def _participants(self) -> ProposeIssueArgs:
        self.participants = _check_participants(self.participants)
        if self.attached:
            # JotFlow reads and snapshots the files itself: never the agent's text.
            raise ValueError("attached is JotFlow's; name files in attachments instead")
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


class RevisedIssue(BaseModel):
    """A new issue's whole revised draft: what replaces the pending one."""

    title: str = Field(min_length=1, max_length=256)
    body: str
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list, description="GitHub logins")
    assignee_reasons: list[AssigneeReason] = Field(
        default_factory=list, max_length=10, description="One reason for each assignee"
    )
    related: list[RelatedIssue] = Field(default_factory=list, max_length=MAX_RELATED)


class ReviseProposalArgs(BaseModel):
    proposal_id: str = Field(description="The pending proposal, from a topic's `pending`")
    base_revision: int = Field(
        ge=0, description="`pending.revision`; a stale one returns the current draft instead"
    )
    issue: RevisedIssue | None = Field(
        default=None, description="For a new issue: its whole revised draft"
    )
    changes: IssueChanges | None = Field(
        default=None, description="For an update: its whole revised changes"
    )
    sources: list[MessageRef] = Field(
        min_length=1, description="The new messages this revision folds in, from this packet"
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _one_draft(self) -> ReviseProposalArgs:
        if (self.issue is None) == (self.changes is None):
            raise ValueError("give exactly one of issue, for a new issue, or changes")
        return self


class ReviseResult(BaseModel):
    proposal_id: str
    revised: bool
    revision: int = Field(description="The proposal's revision now")
    changed: list[str] = Field(default_factory=list, description="Fields the card marks")
    current: PendingDraft | None = Field(
        default=None,
        description="When base_revision was stale: the draft as it stands now. Revise once "
        "more on top of it, with its revision",
    )
    follow_up: bool = Field(
        default=False,
        description="The proposal was settled first; JotFlow drafts these messages again once "
        "it's filed or rejected. Nothing more to do",
    )
    left_off_assignees: list[str] = Field(
        default_factory=list,
        description=(
            "Nominated as a named owner or owner hint, but their GitHub link isn't verified, "
            "so they were left off. Ask in the thread who should own it instead (#305)"
        ),
    )


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


class PointToHelpArgs(BaseModel):
    ts: str = Field(
        description="The message about JotFlow itself, as get_work served it, such as feedback"
    )


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
    left_off_assignees: list[str] = Field(
        default_factory=list,
        description=(
            "Nominated as a named owner or owner hint, but their GitHub link isn't verified, "
            "so they were left off. Ask in the thread who should own it instead (#305)"
        ),
    )


class LinkResult(BaseModel):
    ts: str
    topic_id: str | None
    chatter: bool
    decided_by: Literal["agent", "person"]


class AskResult(BaseModel):
    posted_ts: str


class PointResult(BaseModel):
    posted_ts: str | None = Field(
        default=None, description="The pointer JotFlow posted; none if the thread already had one"
    )


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
    r"|(?P<domain>\b(?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+(?P<last>[^\W\d_]{2,63})\b)"
    r"(?P<path>/[^\s<>|]*)?"
    r")"
)
# File extensions an answer may keep, as in "Node.js" or "config.yaml" (#57): none
# is a top-level domain, so Slack can't make a link of them. An allowlist, so a
# newly delegated TLD fails closed: md, py, sh, rs, io and ai are real ccTLDs, so
# "README.md" is still removed. A test checks this list against a pinned copy of
# IANA's. Only a bare name: anything with a path, a scheme, www. or an @ is removed.
FILE_EXTENSIONS = frozenset(
    {
        "js", "ts", "tsx", "jsx", "mjs", "cjs", "json", "yaml", "yml", "toml",
        "lock", "cfg", "ini", "txt", "csv", "log", "html", "css", "scss", "env",
    }
)  # fmt: skip
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
        last = match.group("last")
        name = match.group(0)
        if (
            last is not None
            and match.group("path") is None
            and name.count(".") == 1  # "evil.com.js" could still hide "evil.com"
            and last.lower() in FILE_EXTENSIONS
        ):
            return name  # a file name, which Slack can't make a link of
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
        authors: Authors | None = None,
        mentions: Mentions | None = None,
        boards: BoardCatalog | None = None,
        files: FileSlack | None = None,
        texts: Texts | None = None,
        names: NamesOf | None = None,
    ) -> None:
        self._store = store
        # JotFlow's own Slack file reader, for attachments (#333).
        self._files = files
        self._policy = policy
        # Who wrote a revision's new messages, from Slack (#304).
        self._authors_of = authors
        self._mentions_of = mentions
        self._text_of = texts
        self._names_of = names
        # The channel's boards, listed and found, and which the write token can update (#309).
        self._board_catalog = boards if boards is not None else BoardCatalog.static(policy)
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

    async def _check_assignees(
        self,
        grant: Grant,
        repo: str,
        assignees: list[str],
        reasons: list[AssigneeReason],
        participants: list[str],
        kept: Sequence[AssigneeReason] = (),
        legacy: Sequence[str] = (),
        found: dict[str, str] | None = None,
    ) -> list[str]:
        """Every assignee is a linked login with one reason JotFlow can check (#305).

        ``found`` collects, by login without case, the name each named_in_text reason
        matched, for the card (#331).

        ``kept`` holds the reasons already on a draft being revised: those stand as they
        are, a person's card add included, so a revision needn't prove them again.
        """
        try:
            check_reasons(assignees, reasons)  # revisions are copied, not validated again
        except ValueError as refused:
            raise ToolError(str(refused)) from None
        left_off: list[str] = []
        if not assignees:
            return left_off
        by_login = {reason.login.casefold(): reason for reason in reasons}
        standing = {(r.login.casefold(), r.why, r.source_ts) for r in kept}
        old = {login.casefold() for login in legacy}
        for login in assignees:
            reason = by_login.get(login.casefold())
            if reason is None and login.casefold() in old:
                continue  # on the draft from before reasons existed: it stands as approved
            if reason is None:
                raise ToolError(
                    f"give assignee {login} a reason: volunteered, named_owner, named_in_text "
                    "or owner_hint"
                )
            if (login.casefold(), reason.why, reason.source_ts) in standing:
                continue
            link = await self._store.identity_for_login(login)
            if link is None:
                raise ToolError(
                    f"assignee {login} hasn't linked a GitHub login in Slack; assign only "
                    "people listed with a github_login"
                )
            if reason.why == "added_on_card":
                raise ToolError("added_on_card is a person's reason, from the card; don't use it")
            if reason.why == "owner_hint":
                hints = self._repos.repo(grant.channel_id, repo).owner_hints or ()
                if link.slack_user_id not in hints:
                    raise ToolError(f"{login} isn't one of {repo}'s owner hints")
                if not _trusted(link):
                    left_off.append(login)
                continue
            assert reason.source_ts is not None  # the model requires it for these two
            ref = await self._packet_ref(grant, reason.source_ts)
            if reason.why == "volunteered":
                if link.slack_user_id not in await self._authors([ref]):
                    raise ToolError(
                        f"{login} didn't write message {reason.source_ts}: a volunteer's "
                        "reason is their own message"
                    )
            elif reason.why == "named_in_text":
                hit = await self._named_in(grant, repo, ref)
                if hit is None or hit[0] != link.slack_user_id:
                    raise ToolError(
                        f"message {reason.source_ts} doesn't name {login} by a name only they "
                        "have here: @-mention them, or ask who should own it"
                    )
                if found is not None:
                    shown = " ".join(without_unsafe_chars(hit[1]).split())[:80]
                    found[login.casefold()] = shown
                if not _trusted(link):
                    left_off.append(login)
            elif link.slack_user_id not in await self._mentioned(ref):
                raise ToolError(
                    f"message {reason.source_ts} doesn't @-mention {login}: a named owner's "
                    "reason is a message naming them"
                )
            elif not _trusted(link):
                left_off.append(login)
        return left_off

    async def _named_in(self, grant: Grant, repo: str, ref: MessageRef) -> tuple[str, str] | None:
        """Who ``ref`` names in words, among the packet's people and the repo's owner hints,
        if exactly one (#331). Names and text come from Slack, never the agent."""
        if self._text_of is None or self._names_of is None:
            raise ToolError("named_in_text isn't available here: @-mention them instead")
        try:
            text = await self._text_of(ref)
        except LookupError:
            raise ToolError(f"message {ref.ts} couldn't be read from Slack") from None
        people: list[str] = []
        for row in await self._store.tool_call_results(grant.run_id, (PACKET_PEOPLE,)):
            people += [user for user in json.loads(row) if isinstance(user, str)]
        hints = self._repos.repo(grant.channel_id, repo).owner_hints or ()
        names = {user: await self._names_of(user) for user in candidates(people, hints)}
        return only(text, names)

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

    async def _attach(self, grant: Grant, file_ids: list[str], body: str) -> list[AttachedFile]:
        """Each named file's text, read again by JotFlow and snapshotted (#333).

        Only a file this run's packet gave text for, still shared in this channel,
        unchanged since, with no credential's shape, and, under issue_links none, no
        Slack link. The issue stays inside GitHub's body limit, or it's refused.
        """
        if not file_ids:
            return []
        if self._policy.attachments_for(grant.channel_id) != "text" or self._files is None:
            raise ToolError(ATTACH_OFF)
        out: list[AttachedFile] = []
        for file_id in dict.fromkeys(file_ids):
            served = await self._store.find_tool_call(grant.run_id, SERVED_FILE, file_id)
            if served is None:
                raise ToolError(f"{file_id} {ATTACH_NOT_SERVED}")
            file = await current_file(file_id, self._files)
            if file is None or grant.channel_id not in file.channels:
                raise ToolError(f"{file_id} {ATTACH_GONE}")
            read = await read_text(file, self._files)
            if read.text is None or read.sha256 is None:
                raise ToolError(f"{file_id}: {read.reason or ATTACH_GONE}")
            if read.sha256 != json.loads(served).get("sha256"):
                raise ToolError(f"{file_id} {ATTACH_CHANGED}")
            try:
                self._check_no_slack_links(read.text)
            except ToolError:
                raise ToolError(f"{file_id} {ATTACH_SLACK_LINK}") from None
            out.append(
                AttachedFile(
                    file_id=file_id,
                    name=file.name,
                    size=len(read.text.encode()),
                    sha256=read.sha256,
                    text=read.text,
                )
            )
        _check_body_with_files(body, out)
        return out

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

    def _boards(self, grant: Grant, boards: list[ProposedBoard]) -> list[ProposedBoard]:
        """Only the channel's own boards, each once, as owner/number (#309).

        The reason is the agent's, so it is cleaned like a question and shown as
        plain text. Boards are in the draft, so approval locks them.
        """
        kept: list[ProposedBoard] = []
        for proposed in boards:
            # Listed, or found by a board scope in a fresh listing (#309 PR 2).
            board = self._board_catalog.board(grant.channel_id, proposed.board)
            if board is None:
                raise ToolError(f"{proposed.board} isn't one of this channel's boards")
            if not self._board_catalog.usable(board):
                raise ToolError(f"{proposed.board} is off: JotFlow's write token can't update it")
            if any(k.board == board.board for k in kept):
                continue
            reason = " ".join(clean_question(proposed.reason).split())[:MAX_BOARD_REASON]
            if not reason:
                raise ToolError("a board's reason has no words left once cleaned")
            kept.append(ProposedBoard(board=board.board, reason=reason))
        return kept

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
            matched: dict[str, str] = {}
            left = await self._check_assignees(
                grant, repo, args.assignees, args.assignee_reasons, args.participants, found=matched
            )
            related, dropped = self._related(grant, args.related)
            attached = await self._attach(grant, args.attachments, args.body)
            assignees, reasons = _without(args.assignees, args.assignee_reasons, left)
            reasons = _with_matched(reasons, matched)
            draft = IssueProposal.model_validate(
                {
                    **args.model_dump(exclude={"participants", "attachments"}),
                    "assignees": assignees,
                    "assignee_reasons": [r.model_dump() for r in reasons],
                    "repo": repo,
                    "labels": labels,
                    "sources": await self._sources(grant, args.sources),
                    "related": [r.model_dump() for r in related],
                    "boards": [b.model_dump() for b in self._boards(grant, args.boards)],
                    "attached": [a.model_dump() for a in attached],
                }
            )
            result = await self._add(grant, draft, args.participants)
            report = {"dropped_related": dropped, "left_off_assignees": left}
            return result.model_copy(update=report)

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
            matched: dict[str, str] = {}
            left = await self._check_assignees(
                grant,
                repo,
                args.changes.add_assignees,
                args.changes.assignee_reasons,
                args.participants,
                found=matched,
            )
            _check_removals(args.changes.remove_labels, args.changes.remove_assignees)
            added, reasons = _without(
                args.changes.add_assignees, args.changes.assignee_reasons, left
            )
            reasons = _with_matched(reasons, matched)
            changes = args.changes.model_copy(
                update={"add_labels": labels, "add_assignees": added, "assignee_reasons": reasons}
            )
            if left and _changes_nothing(changes):
                # Leaving every nominee off left nothing to approve (#325 review).
                raise ToolError(
                    f"nothing is left to update: {', '.join(left)} can't be assigned without "
                    "a verified or admin link. Ask in the thread who should own it instead"
                )
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
            report = {"dropped_related": dropped, "left_off_assignees": left}
            return result.model_copy(update=report)

        return await self._once(grant, "propose_update", args, ProposeResult, act)

    async def revise_proposal(self, grant: Grant, arguments: Any) -> BaseModel:
        """Fold new messages into a pending proposal's draft, as a new revision (#304)."""
        args: ReviseProposalArgs = arguments

        async def act() -> ReviseResult:
            await self._cap(grant, ("revise_proposal",), MAX_REVISIONS_PER_RUN, "revisions")
            record = await self._store.get_proposal(args.proposal_id)
            if record is None or record.channel_id != grant.channel_id:
                raise ToolError(f"no proposal {args.proposal_id} in this channel")
            await self._check_feeds(grant, record)
            sources = await self._sources(grant, args.sources)
            added_by = sorted(await self._authors(sources))
            participants = sorted({*record.participants, *added_by})
            draft = self._revised(grant, record, args, sources)
            matched: dict[str, str] = {}
            left = await self._check_revised(
                grant, draft, participants, record.proposal, found=matched
            )
            draft = _leave_off(draft, left)
            draft = _draft_with_matched(draft, matched)
            try:
                updated = await self._store.revise_proposal(
                    record.id,
                    draft,
                    run_id=grant.run_id,
                    base_revision=args.base_revision,
                    added_by=added_by,
                )
            except StaleRevisionError as stale:
                current = stale.record
                return ReviseResult(
                    proposal_id=record.id,
                    revised=False,
                    revision=current.revision,
                    current=PendingDraft.of(current),
                )
            except TransitionError:
                # Approved, rejected or written first: the follow-up isn't lost (#304).
                await self._store.mark_follow_up_due(record.id)
                return ReviseResult(
                    proposal_id=record.id, revised=False, revision=record.revision, follow_up=True
                )
            except ValueError as refused:
                raise ToolError(str(refused)) from None
            await self._flag_passages(grant, updated.id, updated.proposal)
            [*_, last] = await self._store.proposal_revisions(updated.id)
            return ReviseResult(
                proposal_id=updated.id,
                revised=True,
                revision=updated.revision,
                changed=list(last.changed),
                left_off_assignees=left,
            )

        return await self._once(grant, "revise_proposal", args, ReviseResult, act)

    async def _check_feeds(self, grant: Grant, record: ProposalRecord) -> None:
        """Only a proposal whose topic this run's thread feeds may be revised."""
        topic_id = record.proposal.topic_id
        if record.thread_ts == grant.thread_ts:
            return
        if topic_id is not None:
            found = await self._store.get_topic(topic_id)
            thread = grant.thread_ts
            if found is not None and any(
                thread in (ref.ts, ref.thread_ts) for ref in found.topic.messages
            ):
                return
        raise ToolError(
            f"proposal {record.id} isn't on a topic this thread feeds; link a message first"
        )

    async def _mentioned(self, ref: MessageRef) -> set[str]:
        """Who a message @-mentions, from Slack, never from the agent (#305)."""
        if self._mentions_of is None:
            raise ToolError("a named owner can't be checked here; leave them unassigned")
        try:
            return set(await self._mentions_of(ref))
        except Exception:
            raise ToolError("Slack couldn't say who that message names; try again") from None

    async def _authors(self, sources: list[MessageRef]) -> set[str]:
        """Who wrote the new messages, from Slack, never from the agent."""
        if self._authors_of is None:
            return set()
        try:
            return set(await self._authors_of(sources))
        except Exception:
            raise ToolError("Slack couldn't say who wrote those messages; try again") from None

    def _revised(
        self,
        grant: Grant,
        record: ProposalRecord,
        args: ReviseProposalArgs,
        sources: list[MessageRef],
    ) -> IssueProposal | UpdateProposal:
        old = record.proposal
        if isinstance(old, IssueProposal):
            if args.issue is None:
                raise ToolError("this proposal is a new issue: revise it with `issue`")
            _, labels = self._repo_and_labels(grant, old.repo, args.issue.labels)
            related, _ = self._related(grant, args.issue.related)
            return old.model_copy(
                update={
                    "title": args.issue.title,
                    "body": args.issue.body,
                    "labels": labels,
                    "assignees": args.issue.assignees,
                    "assignee_reasons": args.issue.assignee_reasons,
                    "related": related,
                    "sources": sources,
                    "confidence": args.confidence,
                }
            )
        if args.changes is None:
            raise ToolError("this proposal is an update: revise it with `changes`")
        _, labels = self._repo_and_labels(grant, old.repo, args.changes.add_labels)
        _check_removals(args.changes.remove_labels, args.changes.remove_assignees)
        changes = args.changes.model_copy(update={"add_labels": labels})
        return old.model_copy(
            update={"changes": changes, "sources": sources, "confidence": args.confidence}
        )

    async def _check_revised(
        self,
        grant: Grant,
        draft: IssueProposal | UpdateProposal,
        participants: list[str],
        old: IssueProposal | UpdateProposal,
        found: dict[str, str] | None = None,
    ) -> list[str]:
        """The same rules a new proposal keeps, by the same code. Reasons already on
        the draft stand, a person's card add included (#305). Returns who's left off."""
        if isinstance(draft, IssueProposal):
            await self._check_title(grant, draft.repo, draft.title)
            self._check_no_slack_links(draft.title, draft.body)
            # A revision keeps its files, so its new body must still fit with them.
            _check_body_with_files(draft.body, draft.attached)
            await self._check_mentions(participants, draft.title, draft.body)
            kept = old.assignee_reasons if isinstance(old, IssueProposal) else []
            legacy = _unreasoned(old.assignees, kept) if isinstance(old, IssueProposal) else []
            return await self._check_assignees(
                grant,
                draft.repo,
                draft.assignees,
                draft.assignee_reasons,
                participants,
                kept,
                legacy,
                found,
            )
        changes = draft.changes
        self._check_no_slack_links(changes.body, changes.comment)
        await self._check_mentions(participants, changes.body, changes.comment)
        kept = old.changes.assignee_reasons if isinstance(old, UpdateProposal) else []
        legacy = (
            _unreasoned(old.changes.add_assignees, kept) if isinstance(old, UpdateProposal) else []
        )
        return await self._check_assignees(
            grant,
            draft.repo,
            changes.add_assignees,
            changes.assignee_reasons,
            participants,
            kept,
            legacy,
            found,
        )

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

    async def point_to_help(self, grant: Grant, arguments: Any) -> BaseModel:
        """Point a message about JotFlow itself to ``ask:`` and ``/jotflow help`` (#336).

        The reply is JotFlow's fixed text, never the agent's, posted at most once a thread.
        """
        args: PointToHelpArgs = arguments
        thread = grant.thread_ts
        if thread is None:  # pragma: no cover - drafting grants always name one
            raise ToolError("this run has no thread to reply in")

        async def act() -> PointResult:
            await self._packet_ref(grant, args.ts)
            for already in ("point_to_help", "answer"):
                # One pointer a thread, and none where JotFlow already answered a
                # question, so it can't talk over its own answer (#336 review).
                if await self._store.count_thread_calls(
                    grant.channel_id, thread, already, since=_EVER
                ):
                    return PointResult()
            try:
                posted = await self._poster.post(grant.channel_id, thread, POINT_TO_HELP)
            except PostError as refused:
                raise ToolError(f"Slack did not post the pointer ({refused})") from None
            return PointResult(posted_ts=posted)

        return await self._once(grant, "point_to_help", args, PointResult, act)

    async def answer(self, grant: Grant, arguments: Any) -> BaseModel:
        """A question run's one answer, in its thread, as plain text (#57)."""
        args: AnswerArgs = arguments
        thread = grant.thread_ts
        if thread is None:  # pragma: no cover - question grants always name one
            raise ToolError("this run has no thread to answer in")
        if claims_a_change(args.text):
            # Refused before it counts, so the run can answer the question instead (#329).
            raise ToolError(CLAIMS_A_CHANGE)

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
    "revise_proposal": (
        "Fold new messages into a draft still waiting for approval, as a new revision of the "
        "same card: give the whole revised draft and the new messages as sources. Use "
        "`pending` from the topic, with its revision. If the draft moved on meanwhile, you "
        "get the current one back: revise once more on top of it. The same rules as "
        "propose_issue and propose_update apply. At most 3 a run."
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
    "point_to_help": (
        "For a message about JotFlow itself, such as feedback on it or how to use it: "
        "JotFlow replies in the thread, once, with its own fixed pointer to ask: and "
        "/jotflow help, unless it already answered a question there. You write nothing. "
        "Mark the message chatter too."
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


def _unreasoned(assignees: Sequence[str], reasons: Sequence[AssigneeReason]) -> list[str]:
    """A draft's assignees with no reason: those from before reasons existed (#305)."""
    given = {reason.login.casefold() for reason in reasons}
    return [login for login in assignees if login.casefold() not in given]


def _changes_nothing(changes: IssueChanges) -> bool:
    return (
        changes.body is None
        and changes.comment is None
        and not changes.add_labels
        and not changes.remove_labels
        and not changes.add_assignees
        and not changes.remove_assignees
    )


def _without(
    assignees: Sequence[str], reasons: Sequence[AssigneeReason], left: Sequence[str]
) -> tuple[list[str], list[AssigneeReason]]:
    """The assignees and their reasons, without those left off."""
    gone = {login.casefold() for login in left}
    return (
        [login for login in assignees if login.casefold() not in gone],
        [reason for reason in reasons if reason.login.casefold() not in gone],
    )


def _with_matched(
    reasons: Sequence[AssigneeReason], matched: Mapping[str, str]
) -> list[AssigneeReason]:
    """Each named_in_text reason with the name JotFlow matched, never the agent's (#331)."""
    out: list[AssigneeReason] = []
    for reason in reasons:
        if reason.why == "named_in_text":
            reason = reason.model_copy(update={"matched": matched.get(reason.login.casefold())})
        elif reason.matched is not None:  # pragma: no cover - the model refuses it
            reason = reason.model_copy(update={"matched": None})
        out.append(reason)
    return out


def _draft_with_matched(
    draft: IssueProposal | UpdateProposal, matched: Mapping[str, str]
) -> IssueProposal | UpdateProposal:
    if isinstance(draft, IssueProposal):
        return draft.model_copy(
            update={"assignee_reasons": _with_matched(draft.assignee_reasons, matched)}
        )
    changes = draft.changes.model_copy(
        update={"assignee_reasons": _with_matched(draft.changes.assignee_reasons, matched)}
    )
    return draft.model_copy(update={"changes": changes})


def _leave_off(
    draft: IssueProposal | UpdateProposal, left: Sequence[str]
) -> IssueProposal | UpdateProposal:
    if not left:
        return draft
    if isinstance(draft, IssueProposal):
        assignees, reasons = _without(draft.assignees, draft.assignee_reasons, left)
        return draft.model_copy(update={"assignees": assignees, "assignee_reasons": reasons})
    added, reasons = _without(draft.changes.add_assignees, draft.changes.assignee_reasons, left)
    changes = draft.changes.model_copy(update={"add_assignees": added, "assignee_reasons": reasons})
    return draft.model_copy(update={"changes": changes})


def _trusted(link: Any) -> bool:
    """A link someone proved, or an admin made, with GitHub's account id kept (#305)."""
    made_by_admin = getattr(link, "linked_by", "self") == "admin"
    return bool(link.verified or made_by_admin) and link.github_id is not None


def write_tools(
    store: Store,
    policy: Policy,
    poster: ThreadPoster,
    *,
    clock: Callable[[], datetime] = utc_now,
    new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    repos: RepoCatalog | None = None,
    docs: DocReads | None = None,
    authors: Authors | None = None,
    mentions: Mentions | None = None,
    boards: BoardCatalog | None = None,
    files: FileSlack | None = None,
    texts: Texts | None = None,
    names: NamesOf | None = None,
) -> tuple[ToolSpec, ...]:
    """The write tools, bound to their dependencies. Built once at startup."""
    tools = WriteTools(
        store,
        policy,
        poster,
        clock=clock,
        new_id=new_id,
        repos=repos,
        docs=docs,
        authors=authors,
        mentions=mentions,
        texts=texts,
        names=names,
        boards=boards,
        files=files,
    )
    plan: tuple[tuple[str, type[BaseModel], frozenset[RunKind]], ...] = (
        ("propose_issue", ProposeIssueArgs, DRAFTING),
        ("propose_update", ProposeUpdateArgs, DRAFTING),
        ("revise_proposal", ReviseProposalArgs, DRAFTING),
        ("link_message", LinkMessageArgs, DRAFTING),
        ("ask_channel", AskChannelArgs, DRAFTING),
        ("point_to_help", PointToHelpArgs, DRAFTING),
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
