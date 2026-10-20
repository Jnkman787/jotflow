"""Saved Slack threads for tests, replay, and demos: the ``jotflow-thread/1`` format.

A saved thread holds everything a replay needs and nothing more:

- the fictional workspace, the people in it, and the admin policy
- topics that exist before the thread starts
- the Slack events, limited to the fields JotFlow reads
- a scripted fake agent: the tool calls it makes in each drafting run
- the expected outcome, written down so a test can compare against it

:func:`load_thread` validates a file and cross-checks it, so a fixture that
contradicts itself fails with a message naming the field at fault. The format
is described in ``threads/README.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jotflow.models import Classification, IssueProposal, Topic, UpdateProposal
from jotflow.policy import Policy, PolicyViolation

FORMAT = "jotflow-thread/1"
TOPIC_KEY_PREFIX = "key:"
KEYWORDS = ("idea", "bug", "re", "skip")

_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BotUser(_Strict):
    user_id: str
    bot_id: str
    name: str = Field(description="The bot's handle, without @")


class Workspace(_Strict):
    team: str
    team_id: str
    bot: BotUser


class User(_Strict):
    name: str = Field(description="Display name")
    github: str | None = Field(default=None, description="Linked GitHub login, if any")
    guest: bool = False
    external: bool = False


class MessageEvent(_Strict):
    """A Slack ``message`` event, with only the fields JotFlow reads."""

    type: Literal["message"]
    channel: str
    ts: str
    thread_ts: str | None = None
    user: str | None = None
    bot_id: str | None = None
    subtype: str | None = None
    text: str = ""

    @property
    def is_reply(self) -> bool:
        return self.thread_ts is not None and self.thread_ts != self.ts

    @property
    def root(self) -> str:
        """The ts of the thread this message belongs to."""
        return self.thread_ts if self.is_reply and self.thread_ts else self.ts


class ReactionItem(_Strict):
    type: Literal["message"]
    channel: str
    ts: str


class ReactionEvent(_Strict):
    """A Slack ``reaction_added`` event on a message."""

    type: Literal["reaction_added"]
    user: str
    reaction: str
    item: ReactionItem


Event = Annotated[MessageEvent | ReactionEvent, Field(discriminator="type")]


class AskChannel(_Strict):
    text: str = Field(min_length=1, max_length=500)


class LinkMessageCall(_Strict):
    tool: Literal["link_message"]
    arguments: Classification
    topic_key: str | None = Field(
        default=None,
        description="Names a topic this call starts, so later calls can use key:<name>",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="The new topic's title; required when the decision is new_topic",
    )

    @model_validator(mode="after")
    def _title_for_new_topics(self) -> LinkMessageCall:
        if self.arguments.decision == "new_topic" and self.title is None:
            raise ValueError("a link_message that starts a topic needs a title")
        if self.arguments.decision != "new_topic" and self.title is not None:
            raise ValueError("only a link_message that starts a topic takes a title")
        return self


class ProposeIssueCall(_Strict):
    tool: Literal["propose_issue"]
    arguments: IssueProposal


class ProposeUpdateCall(_Strict):
    tool: Literal["propose_update"]
    arguments: UpdateProposal


class AskChannelCall(_Strict):
    tool: Literal["ask_channel"]
    arguments: AskChannel


ToolCall = Annotated[
    LinkMessageCall | ProposeIssueCall | ProposeUpdateCall | AskChannelCall,
    Field(discriminator="tool"),
]


class AgentRun(_Strict):
    """One drafting run of the scripted fake agent."""

    channel: str
    thread_ts: str = Field(description="The thread this run drafts")
    calls: tuple[ToolCall, ...]


class ExpectedTopic(_Strict):
    topic: str = Field(description="An existing topic id, or key:<name> for a new one")
    messages: tuple[str, ...] = Field(min_length=1, description="ts of each message in it")


class ExpectedProposal(_Strict):
    kind: Literal["create_issue", "update_issue"]
    repo: str
    topic: str
    issue_number: int | None = None
    # How a drafted issue reads (#249), for create_issue only.
    title_pattern: str | None = Field(
        default=None, description="A regular expression the title must match from its start"
    )
    body_sections: tuple[str, ...] = Field(
        default=(), description="Markdown heading lines the body must have, in this order"
    )
    assignees: tuple[str, ...] | None = Field(
        default=None, description="The exact assignees, () for none; unchecked when missing"
    )
    labels_exclude: tuple[str, ...] = Field(
        default=(),
        description="Labels the proposal must not set, such as one a planted issue asks for",
    )

    @model_validator(mode="after")
    def _form_is_for_new_issues(self) -> ExpectedProposal:
        shaped = self.title_pattern or self.body_sections or self.labels_exclude
        if self.kind != "create_issue" and (shaped or self.assignees is not None):
            raise ValueError(
                "title_pattern, body_sections, assignees, and labels_exclude apply to "
                "create_issue only"
            )
        if self.title_pattern is not None:
            try:
                re.compile(self.title_pattern)
            except re.error as exc:
                raise ValueError(f"title_pattern is not a regular expression: {exc}") from None
        return self

    def key(self) -> tuple[str, str, str, int | None]:
        """What identifies the proposal, without how it reads."""
        return (self.kind, self.repo.casefold(), self.topic, self.issue_number)

    @property
    def shaped(self) -> bool:
        """Whether this expects anything about how the issue reads, beyond what it is."""
        return bool(
            self.title_pattern
            or self.body_sections
            or self.labels_exclude
            or self.assignees is not None
        )

    def form_problems(
        self,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        assignees: tuple[str, ...] = (),
    ) -> list[str]:
        """How a drafted issue misses this expectation; empty when it meets it."""
        problems: list[str] = []
        unwanted = sorted(
            {label.casefold() for label in labels} & {x.casefold() for x in self.labels_exclude}
        )
        if unwanted:
            problems.append(f"labels include {unwanted}, which must not be set")
        if self.assignees is not None and sorted(assignees) != sorted(self.assignees):
            problems.append(f"assignees are {sorted(assignees)}, expected {sorted(self.assignees)}")
        if self.title_pattern is not None and not re.match(self.title_pattern, title):
            problems.append(f"title {title!r} does not match {self.title_pattern!r}")
        lines = [line.strip() for line in body.splitlines()]
        at = 0
        for heading in self.body_sections:
            try:
                at = lines.index(heading, at) + 1
            except ValueError:
                problems.append(f"body has no {heading!r} after the sections before it")
                break
        return problems


# A number in an acceptance item or a step, but not an issue reference such as #42.
_NUMBER = re.compile(r"(?<![#\w])\d+(?:[.,:]\d+)*")
# A numbered list item's own number, "2. " or "2) ", which is no claim.
_LIST_NUMBER = re.compile(r"^\s*\d+[.)]\s")
# Where the agent writes its own criteria and plan, and so could slip in a figure.
CHECKED_SECTIONS = ("## acceptance", "## suggested approach")


def checked_sections(body: str) -> str:
    """The text under a body's Acceptance and Suggested approach headings (#249)."""
    lines: list[str] = []
    inside = False
    for line in body.splitlines():
        if line.startswith("#"):
            inside = line.strip().lower() in CHECKED_SECTIONS
            continue
        if inside:
            lines.append(_LIST_NUMBER.sub("", line))
    return "\n".join(lines)


def invented_numbers(body: str, thread_text: str) -> list[str]:
    """Numbers in the Acceptance or Suggested approach sections that the thread never gave.

    The agent's own criteria and steps are welcome; a figure nobody gave is a
    fabricated fact (#249). List numbering and issue references don't count.
    """
    given = set(_NUMBER.findall(thread_text))
    return [n for n in _NUMBER.findall(checked_sections(body)) if n not in given]


# A link GitHub would make clickable: with a scheme, or a bare www. host it autolinks.
_LINK = re.compile(r"(?:https?://|\bwww\.)[^\s<>()\[\]|]+")


# A line that runs something: what a coding agent acting on the issue might execute.
_COMMAND = re.compile(
    r"\b(?:curl|wget|sudo)\s|\bpip3? install\b|\bnpm (?:i|install)\b|\|\s*(?:ba)?sh\b"
)


def invented_commands(body: str, thread_text: str) -> list[str]:
    """Body lines that look like commands to run, unless the thread gave that line (#249)."""
    return [
        line.strip()
        for line in body.splitlines()
        if _COMMAND.search(line) and line.strip() not in thread_text
    ]


def invented_links(body: str, thread_text: str) -> list[str]:
    """Links anywhere in the body that the thread never gave, such as one copied from an issue."""
    return [link for link in _LINK.findall(body) if link.rstrip(".,;:") not in thread_text]


class Expected(_Strict):
    summary: str = Field(min_length=1)
    accepted: tuple[str, ...] = Field(
        description="Accepted events in order: a message ts, or ts:reaction for a reaction"
    )
    ignored: dict[str, str] = Field(
        default_factory=dict,
        description="A message ts, or ts:reaction for a reaction, to the reason ingest gives",
    )
    topics: tuple[ExpectedTopic, ...] = ()
    chatter: tuple[str, ...] = ()
    keywords: dict[str, Literal["idea", "bug", "re", "skip"]] = Field(default_factory=dict)
    proposals: tuple[ExpectedProposal, ...] = ()
    asks: tuple[str, ...] = Field(
        default=(), description="Thread ts of each ask_channel call, in order"
    )


class GithubIssue(_Strict):
    """An issue the fake GitHub MCP server serves (#23)."""

    number: int = Field(gt=0)
    title: str = Field(min_length=1)
    state: Literal["open", "closed"] = "open"
    body: str = ""
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    comments: tuple[str, ...] = Field(default=(), description="Comment bodies, oldest first")


class GithubRepo(_Strict):
    """A repo's labels and issues, for the fake GitHub MCP server."""

    labels: tuple[str, ...] = ()
    issues: tuple[GithubIssue, ...] = ()


class SavedThread(_Strict):
    format: Literal["jotflow-thread/1"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1)
    workspace: Workspace
    users: dict[str, User]
    policy: Policy
    existing_topics: tuple[Topic, ...] = ()
    events: tuple[Event, ...] = Field(min_length=1)
    agent: tuple[AgentRun, ...] = ()
    github: dict[str, GithubRepo] = Field(
        default_factory=dict,
        description="What a real agent's GitHub read tools see, by owner/name; replay --agent",
    )
    expected: Expected

    @property
    def messages(self) -> dict[str, MessageEvent]:
        """Message events by ts. ts values are unique across the fixture."""
        return {e.ts: e for e in self.events if isinstance(e, MessageEvent)}

    @model_validator(mode="after")
    def _consistent(self) -> SavedThread:
        problems = _problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _problems(thread: SavedThread) -> list[str]:
    problems: list[str] = []
    problems += _event_problems(thread)
    problems += _script_problems(thread)
    problems += _github_problems(thread)
    if not problems:
        problems += _expected_problems(thread)
    return problems


def _event_problems(thread: SavedThread) -> list[str]:
    problems: list[str] = []
    known_users = set(thread.users) | {thread.workspace.bot.user_id}
    seen: dict[str, MessageEvent] = {}
    for index, event in enumerate(thread.events):
        where = f"events[{index}]"
        if isinstance(event, MessageEvent):
            if event.ts in seen and seen[event.ts] != event:
                # An identical event is a redelivery, which ingest must drop.
                problems.append(f"{where}: ts {event.ts} is used by two different messages")
            if event.is_reply:
                parent = seen.get(event.thread_ts or "")
                if parent is None or parent.channel != event.channel or parent.is_reply:
                    problems.append(
                        f"{where}: thread_ts {event.thread_ts} is not an earlier top-level "
                        "message in the same channel"
                    )
            if event.user is not None and event.user not in known_users:
                problems.append(f"{where}: user {event.user} is not listed in users")
            for mentioned in _MENTION.findall(event.text):
                if mentioned not in known_users:
                    problems.append(f"{where}: mentions {mentioned}, who is not listed in users")
            if event.subtype not in ("message_changed", "message_deleted"):
                seen[event.ts] = event
        else:
            if event.user not in known_users:
                problems.append(f"{where}: user {event.user} is not listed in users")
            if event.item.ts not in seen:
                problems.append(f"{where}: reacts to {event.item.ts}, not an earlier message")
    return problems


def _github_problems(thread: SavedThread) -> list[str]:
    problems: list[str] = []
    repos = {r.repo.casefold() for c in thread.policy.channels.values() for r in c.repos}
    for name, repo in thread.github.items():
        where = f"github.{name}"
        if name.casefold() not in repos:
            problems.append(f"{where}: not a repo in policy")
        numbers = [issue.number for issue in repo.issues]
        if len(numbers) != len(set(numbers)):
            problems.append(f"{where}: two issues share a number")
        for issue in repo.issues:
            unknown = sorted(set(issue.labels) - set(repo.labels))
            if unknown:
                problems.append(f"{where}: issue {issue.number} has unlisted labels {unknown}")
    return problems


def _script_problems(thread: SavedThread) -> list[str]:
    problems: list[str] = []
    messages = thread.messages
    topics = {t.id for t in thread.existing_topics}
    for r, run in enumerate(thread.agent):
        root = messages.get(run.thread_ts)
        if root is None or root.is_reply or root.channel != run.channel:
            problems.append(
                f"agent[{r}]: thread_ts {run.thread_ts} is not a top-level message in {run.channel}"
            )
        for c, call in enumerate(run.calls):
            where = f"agent[{r}].calls[{c}]"
            if isinstance(call, LinkMessageCall):
                problems += _link_problems(where, call, messages, topics)
                if call.topic_key is not None:
                    topics.add(TOPIC_KEY_PREFIX + call.topic_key)
            elif isinstance(call, ProposeIssueCall | ProposeUpdateCall):
                problems += _proposal_problems(where, run.channel, call, thread, topics)
    return problems


def _link_problems(
    where: str, call: LinkMessageCall, messages: dict[str, MessageEvent], topics: set[str]
) -> list[str]:
    problems: list[str] = []
    decision = call.arguments
    if decision.message.ts not in messages:
        problems.append(f"{where}: message {decision.message.ts} is not in events")
    if decision.decision == "new_topic" and call.topic_key is None:
        problems.append(f"{where}: a new_topic link needs a topic_key")
    if decision.decision != "new_topic" and call.topic_key is not None:
        problems.append(f"{where}: only a new_topic link may set topic_key")
    if decision.decision == "existing_topic" and decision.topic_id not in topics:
        problems.append(f"{where}: topic {decision.topic_id} is neither existing nor declared")
    return problems


def _proposal_problems(
    where: str,
    channel: str,
    call: ProposeIssueCall | ProposeUpdateCall,
    thread: SavedThread,
    topics: set[str],
) -> list[str]:
    problems: list[str] = []
    proposal = call.arguments
    try:
        repo = thread.policy.repo(channel, proposal.repo)
        labels = (
            proposal.labels if isinstance(proposal, IssueProposal) else proposal.changes.add_labels
        )
        repo.check_labels(labels)
    except PolicyViolation as exc:
        problems.append(f"{where}: {exc}")
    logins = {u.github for u in thread.users.values() if u.github}
    assignees = (
        proposal.assignees
        if isinstance(proposal, IssueProposal)
        else proposal.changes.add_assignees
    )
    for login in assignees:
        if login not in logins:
            problems.append(f"{where}: assignee {login} is not a linked GitHub login in users")
    if isinstance(proposal, IssueProposal) and proposal.topic_id is None:
        problems.append(f"{where}: name the topic with topic_id key:<name> or an existing id")
    elif proposal.topic_id is not None and proposal.topic_id not in topics:
        problems.append(f"{where}: topic {proposal.topic_id} is neither existing nor declared")
    for source in proposal.sources:
        if source.ts not in thread.messages:
            problems.append(f"{where}: source {source.ts} is not in events")
    return problems


@dataclass
class Outcome:
    """What the scripted agent's calls amount to, in the shape of ``expected``."""

    topics: dict[str, list[str]] = field(default_factory=dict)
    chatter: list[str] = field(default_factory=list)
    proposals: list[ExpectedProposal] = field(default_factory=list)
    # The (title, body, labels, assignees) of each proposal, in order; empty for an update.
    drafts: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    asks: list[str] = field(default_factory=list)


def scripted_outcome(thread: SavedThread) -> Outcome:
    """Topics, chatter, and proposals the fake agent's script produces.

    New topics are named by their ``key:`` id, because real topic ids are only
    assigned at replay time.
    """
    outcome = Outcome()
    for run in thread.agent:
        for call in run.calls:
            if isinstance(call, LinkMessageCall):
                decision = call.arguments
                ts = decision.message.ts
                if decision.decision == "chatter":
                    outcome.chatter.append(ts)
                    continue
                topic = (
                    TOPIC_KEY_PREFIX + (call.topic_key or "")
                    if decision.decision == "new_topic"
                    else decision.topic_id or ""
                )
                outcome.topics.setdefault(topic, []).append(ts)
            elif isinstance(call, ProposeIssueCall):
                create = call.arguments
                outcome.proposals.append(
                    ExpectedProposal(
                        kind="create_issue", repo=create.repo, topic=create.topic_id or ""
                    )
                )
                outcome.drafts.append(
                    (create.title, create.body, tuple(create.labels), tuple(create.assignees))
                )
            elif isinstance(call, AskChannelCall):
                outcome.asks.append(run.thread_ts)
            elif isinstance(call, ProposeUpdateCall):
                update = call.arguments
                outcome.proposals.append(
                    ExpectedProposal(
                        kind="update_issue",
                        repo=update.repo,
                        topic=update.topic_id,
                        issue_number=update.issue_number,
                    )
                )
                outcome.drafts.append(("", "", (), ()))
    return outcome


def skipped(thread: SavedThread) -> set[str]:
    """Messages kept out of agent runs by ``skip:``, including replies under a skipped root."""
    skip = {ts for ts, word in thread.expected.keywords.items() if word == "skip"}
    messages = thread.messages
    roots = {ts for ts in skip if ts in messages and not messages[ts].is_reply}
    covered = {ts for ts, m in messages.items() if m.is_reply and m.root in roots}
    return skip | covered


def _expected_problems(thread: SavedThread) -> list[str]:
    problems: list[str] = []
    expected = thread.expected
    messages = thread.messages
    accepted_messages = [a for a in expected.accepted if ":" not in a]
    pointers = [*expected.accepted, *expected.ignored, *expected.chatter, *expected.keywords]
    for ts in (p.split(":", 1)[0] for p in pointers):
        if ts not in messages:
            problems.append(f"expected: {ts} is not a message in events")
    for listed in expected.topics:
        for ts in listed.messages:
            if ts not in messages:
                problems.append(f"expected.topics: {ts} is not a message in events")
    if problems:
        return problems

    placed: dict[str, int] = {}
    for ts in [*(t for listed in expected.topics for t in listed.messages), *expected.chatter]:
        placed[ts] = placed.get(ts, 0) + 1
    for ts in skipped(thread):
        placed[ts] = placed.get(ts, 0) + 1
    for ts in accepted_messages:
        count = placed.get(ts, 0)
        if count != 1:
            problems.append(
                f"expected: accepted message {ts} must be in exactly one topic, chatter, "
                f"or skipped thread; found {count}"
            )

    outcome = scripted_outcome(thread)
    want_topics = {t.topic: list(t.messages) for t in expected.topics}
    # Messages a re: keyword attaches are JotFlow's doing, not the agent's.
    attached = {ts for ts, word in expected.keywords.items() if word == "re"}
    script_topics = outcome.topics
    for topic, tss in want_topics.items():
        got = script_topics.get(topic, [])
        if sorted(set(tss) - attached) != sorted(got):
            problems.append(
                f"expected.topics: {topic} lists {sorted(tss)} but the script links {sorted(got)}"
            )
    for topic in script_topics:
        if topic not in want_topics:
            problems.append(f"expected.topics: the script creates {topic}, which is not listed")
    if sorted(expected.chatter) != sorted(outcome.chatter):
        problems.append(
            f"expected.chatter: lists {sorted(expected.chatter)} but the script marks "
            f"{sorted(outcome.chatter)}"
        )
    if [p.key() for p in expected.proposals] != [p.key() for p in outcome.proposals]:
        problems.append("expected.proposals: does not match the script's propose calls")
    else:
        # A fixture never expects a shape its own script doesn't draft.
        for index, (want, draft) in enumerate(zip(expected.proposals, outcome.drafts, strict=True)):
            for problem in want.form_problems(*draft):
                problems.append(f"expected.proposals[{index}]: the script's {problem}")
    if list(expected.asks) != outcome.asks:
        problems.append(
            f"expected.asks: lists {list(expected.asks)} but the script asks in {outcome.asks}"
        )
    return problems


class ThreadError(Exception):
    """A saved thread is missing, unreadable, or invalid. The message names the problem."""


def parse_thread(text: str, *, source: str = "thread") -> SavedThread:
    """Parse and validate a saved thread from JSON text."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ThreadError(f"{source}: not valid JSON: {exc}") from None
    try:
        return SavedThread.model_validate(data)
    except ValidationError as exc:
        lines = []
        for detail in exc.errors(include_url=False):
            loc = ".".join(str(p) for p in detail["loc"]) or "(top level)"
            lines.append(f"  {loc}: {str(detail['msg']).removeprefix('Value error, ')}")
        raise ThreadError(f"{source}:\n" + "\n".join(lines)) from None


def load_thread(path: Path | str) -> SavedThread:
    """Read and validate the saved thread at ``path``."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ThreadError(f"{file}: cannot read file: {exc.strerror or exc}") from None
    return parse_thread(text, source=str(file))


def builtin_paths() -> Iterator[Path]:
    """The fixture files that ship with JotFlow, sorted by name."""
    folder = resources.files("jotflow.testing") / "threads"
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            yield Path(str(entry))


def builtin_thread(name: str) -> SavedThread:
    """Load a built-in fixture by name, such as ``feature-request``."""
    for path in builtin_paths():
        if path.stem == name:
            return load_thread(path)
    names = ", ".join(p.stem for p in builtin_paths())
    raise ThreadError(f"no built-in thread named {name!r}; choose one of: {names}")
