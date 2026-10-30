"""Admin policy, loaded from ``jotflow.toml``.

This is the policy tier of D2: values an admin owns and changes by editing a
file deployed with the service. Day-to-day state, such as channel mode and
identity links, lives in the database instead.

Every key is checked. An unknown or misspelt key is an error, and every error
names the key it is about, for example ``channels.C0EXAMPLE1.repos[0].nickname``.
See ``examples/jotflow.example.toml`` for a complete file.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from jotflow.doc_paths import PathRefused, check_patterns
from jotflow.models import RepoName, RepoVisibility, is_github_login, shown, under_owner
from jotflow.reactions import DEFAULT_APPROVAL, DEFAULT_ISSUE_REACTION, KEYWORD_REACTIONS, reserved
from jotflow.text import unsafe_text_problem
from jotflow.title_pattern import check_title_pattern

_CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{2,}")
_USER_ID = re.compile(r"[UW][A-Z0-9]{2,}")
_NICKNAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# Public, for other modules that check a nickname the same way (#327 review).
NICKNAME_PATTERN = _NICKNAME
_PRESET = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _matching(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"{shown(value)} is not {what}")
        return value

    return AfterValidator(check)


ChannelId = Annotated[str, _matching(_CHANNEL_ID, "a Slack channel id such as C0123ABCD")]
UserId = Annotated[str, _matching(_USER_ID, "a Slack user id such as U0123ABCD")]
Nickname = Annotated[
    str,
    _matching(_NICKNAME, "a nickname of lowercase letters, digits, - or _, up to 32 characters"),
]
PresetName = Annotated[str, _matching(_PRESET, "a preset name of lowercase letters, digits and -")]
# A Slack emoji name without colons. Custom emoji names may be up to 100 characters.
_EMOJI_NAME = re.compile(r"[a-z0-9_+-]{1,100}")
EmojiName = Annotated[
    str, _matching(_EMOJI_NAME, "a Slack emoji name, such as heavy_check_mark, without colons")
]
MAX_LABEL_LENGTH = 50  # GitHub's limit on a label name


def label_problem(label: str) -> str | None:
    """Why ``label`` cannot be a label name, or None if it can.

    Labels reach the Slack card and GitHub. With no label list they come from
    the agent, so control characters, line breaks, bidirectional overrides,
    invisible format characters, and surrounding spaces are refused here
    rather than trusted downstream.
    """
    if not 0 < len(label) <= MAX_LABEL_LENGTH:
        return f"must be 1 to {MAX_LABEL_LENGTH} characters"
    if label != label.strip():
        return "must not start or end with a space"
    return unsafe_text_problem(label)


def _check_label_name(value: str) -> str:
    if problem := label_problem(value):
        raise ValueError(f"label {shown(value)} {problem}")
    return value


Label = Annotated[str, AfterValidator(_check_label_name)]

IssueLinks = Literal["permalinks", "none"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen and value not in dupes:
            dupes.append(value)
        seen.add(key)
    return dupes


class PolicyViolation(Exception):
    """A request that the loaded policy does not allow."""


class UnknownChannelError(PolicyViolation):
    def __init__(self, channel_id: str) -> None:
        super().__init__(f"channel {channel_id} is not configured in jotflow.toml")
        self.channel_id = channel_id


class RepoNotAllowedError(PolicyViolation):
    def __init__(self, channel_id: str, repo: str) -> None:
        super().__init__(f"repo {repo!r} is not one of channel {channel_id}'s repos")
        self.channel_id = channel_id
        self.repo = repo


class LabelNotAllowedError(PolicyViolation):
    def __init__(self, repo: str, labels: list[str]) -> None:
        listed = ", ".join(repr(label) for label in labels)
        super().__init__(f"labels not in {repo}'s label list: {listed}")
        self.repo = repo
        self.labels = labels


class InvalidLabelError(PolicyViolation):
    def __init__(self, repo: str, labels: list[str]) -> None:
        listed = "; ".join(f"{shown(label)} {label_problem(label)}" for label in labels)
        super().__init__(f"not valid label names for {repo}: {listed}")
        self.repo = repo
        self.labels = labels


# A team's conventions text for one repo, in characters (#216).
MAX_CONVENTIONS = 2000
# A channel's triage guide, in characters (#317).
MAX_TRIAGE_GUIDE = 8000


def _plain_policy_text(name: str, value: str | None, empty: str) -> str | None:
    """Admin-written text the agent follows: never empty, and every line free of
    invisible or bidi characters, so a reviewer of jotflow.toml sees all of it."""
    if value is None:
        return None
    if not value.strip():
        raise ValueError(f"{name} is empty; remove the line, or {empty}")
    for line in value.splitlines():
        if line.strip() and (problem := unsafe_text_problem(line.strip())):
            raise ValueError(f"{name} {problem}")
    return value.strip()


class RepoPolicy(_Strict):
    """One repo a channel may feed (D11)."""

    repo: RepoName = Field(description="owner/name")
    nickname: Nickname = Field(description="Short name people use, as in `re: web#42`")
    description: str = Field(
        min_length=1, max_length=200, description="What the repo covers; the agent picks by this"
    )
    labels: tuple[Label, ...] | None = Field(
        default=None,
        description=(
            "Optional. Narrows the labels a proposal may set to this list. Missing means "
            "any label that already exists on the repo; an empty list is an error"
        ),
    )
    visibility: RepoVisibility | None = Field(
        default=None,
        description=(
            "Optional. public or private on GitHub. public puts a warning on every card for "
            "this repo; the agent can add that warning but never remove it (D7, #122)"
        ),
    )
    conventions: str | None = Field(
        default=None,
        max_length=MAX_CONVENTIONS,
        description=(
            "Optional. The team's own rules for this repo's issues, such as required "
            "sections or label norms, in plain text. The drafting agent follows them over "
            "the style of the repo's recent issues (#216)"
        ),
    )
    title_pattern: str | None = Field(
        default=None,
        description=(
            "Optional. A regular expression every proposed title must fully match, such as "
            "^(feat|fix|docs)(\\([a-z-]+\\))?: .+$. Only patterns that always match "
            "quickly are allowed (#216)"
        ),
    )

    read_paths: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Optional. The repo's text files a drafting run may read, as patterns such as "
            "docs/plans/**/*.md. Needs Contents: Read on the read token, which then covers "
            "every repo that token reaches (#209)"
        ),
    )
    owner_hints: tuple[UserId, ...] | None = Field(
        default=None,
        max_length=20,
        description=(
            "Optional. Slack member IDs of people who own this repo's area. The agent may "
            "propose one as an assignee, saying so on the card, when an idea plainly falls "
            "to this repo (#305). Only someone with a linked GitHub login is ever assigned"
        ),
    )

    @field_validator("read_paths")
    @classmethod
    def _checked_read_paths(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        try:
            return check_patterns(value)
        except PathRefused as refused:
            raise ValueError(str(refused)) from None

    @field_validator("conventions")
    @classmethod
    def _plain_conventions(cls, value: str | None) -> str | None:
        return _plain_policy_text("conventions", value, "write the rules")

    @field_validator("title_pattern")
    @classmethod
    def _cheap_title_pattern(cls, value: str | None) -> str | None:
        return None if value is None else check_title_pattern(value)

    @field_validator("labels")
    @classmethod
    def _no_empty_list(cls, labels: tuple[str, ...] | None) -> tuple[str, ...] | None:
        # None, or a missing key, means any existing label. An admin who writes
        # an empty list may mean "no labels", so it is an error, not a silent "any".
        if labels is not None and not labels:
            raise ValueError(
                "an empty labels list is ambiguous; remove the labels line to allow any "
                "existing label, or list the labels to allow"
            )
        return labels

    @model_validator(mode="after")
    def _unique_labels(self) -> RepoPolicy:
        if self.labels and (dupes := _duplicates(self.labels)):
            raise ValueError(f"labels listed more than once: {', '.join(dupes)}")
        return self

    @property
    def restricts_labels(self) -> bool:
        """Whether a label list narrows what proposals may set."""
        return self.labels is not None

    def check_labels(self, labels: Iterable[str]) -> list[str]:
        """The labels a proposal may set, without duplicates.

        With no label list, any valid label name is allowed. JotFlow cannot see
        which labels exist on the repo; the agent is instructed to use only
        existing ones (#23), and the execution run checks before applying (#31).
        With a list, labels must be on it, matched without case, and come back in
        the list's own spelling. Raises :class:`LabelNotAllowedError` for a label
        outside the list, and :class:`InvalidLabelError` for one no repo could have.
        """
        wanted = list(labels)
        invalid = [label for label in wanted if label_problem(label) is not None]
        if invalid:
            raise InvalidLabelError(self.repo, invalid)
        if not self.restricts_labels:
            unique: list[str] = []
            seen: set[str] = set()
            for label in wanted:
                if label.casefold() not in seen:
                    seen.add(label.casefold())
                    unique.append(label)
            return unique
        allowed = {label.casefold(): label for label in self.labels or ()}
        rejected = [label for label in wanted if label.casefold() not in allowed]
        if rejected:
            raise LabelNotAllowedError(self.repo, rejected)
        canonical: list[str] = []
        for label in wanted:
            if allowed[label.casefold()] not in canonical:
                canonical.append(allowed[label.casefold()])
        return canonical


class HelpPolicy(_Strict):
    """How JotFlow helps people in a channel (#80). Each one is on by default."""

    guide: bool = Field(default=True, description="Post and pin a short guide once per channel")
    newcomer_tip: bool = Field(
        default=True, description="A one-time private tip on a person's first message"
    )
    near_miss_nudge: bool = Field(
        default=True, description="A private nudge, once per person, for a mistyped keyword"
    )


class QuestionsPolicy(_Strict):
    """Questions asked with ``ask:`` or an @-mention of the bot (#57). On by default."""

    enabled: bool = Field(default=True, description="Answer questions at all")
    per_hour: int = Field(
        default=10, ge=1, le=1000, description="Question runs one channel may start in an hour"
    )
    per_person_seconds: int = Field(
        default=60, ge=0, le=86400, description="How long one person waits between questions"
    )


class ChannelQuestions(_Strict):
    """A channel's own question settings. Each one left out follows the top-level one."""

    enabled: bool | None = None
    per_hour: int | None = Field(default=None, ge=1, le=1000)
    per_person_seconds: int | None = Field(default=None, ge=0, le=86400)


class ChannelHelp(_Strict):
    """A channel's own help settings. Each one left out follows the top-level ``[help]``."""

    guide: bool | None = None
    newcomer_tip: bool | None = None
    near_miss_nudge: bool | None = None


# A GitHub owner, written out: never a pattern, so a scope can't reach another (#236).
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
# A repo name as GitHub allows it, for an alias's target.
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
# A glob on a repo name: its characters, plus *, ?, [, ] and !.
_GLOB = re.compile(r"[A-Za-z0-9._*?\[\]!-]{1,100}")
MAX_SCOPES = 5
MAX_GLOBS = 20

Owner = Annotated[str, _matching(_OWNER, "a GitHub owner such as example-org, not a pattern")]
NameGlob = Annotated[str, _matching(_GLOB, "a glob on a repo name, such as web-*")]
RepoBareName = Annotated[str, _matching(_REPO_NAME, "a repo name such as website-v2")]


class ScopePolicy(_Strict):
    """Every repo of one owner whose name matches, found live (#236).

    The policy stays the write allowlist: the owner is fixed, and only names the
    globs match, and none excludes, are allowed.
    """

    owner: Owner = Field(description="The GitHub org or user, written out")
    match: tuple[NameGlob, ...] = Field(
        default=("*",), min_length=1, max_length=MAX_GLOBS, description="Globs on repo names"
    )
    exclude: tuple[NameGlob, ...] = Field(
        default=(), max_length=MAX_GLOBS, description="Globs on repo names to leave out"
    )
    include_forks: bool = Field(default=False, description="Whether forks are in scope")
    include_archived: bool = Field(default=False, description="Whether archived repos are")
    labels: tuple[Label, ...] | None = Field(
        default=None, description="Optional. Narrows the labels for every repo in the scope"
    )
    aliases: dict[Nickname, RepoBareName] = Field(
        default_factory=dict, description="Optional nicknames, mapped to repo names"
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional. The team's own words for the scope's repos, shown first",
    )

    @field_validator("labels")
    @classmethod
    def _no_empty_labels(cls, labels: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if labels is not None and not labels:
            raise ValueError("an empty labels list is ambiguous; remove the line to allow any")
        return labels

    @field_validator("description")
    @classmethod
    def _plain_description(cls, value: str | None) -> str | None:
        if value is not None and (problem := unsafe_text_problem(value)):
            raise ValueError(f"description {problem}")
        return value

    def covers(self, name: str) -> bool:
        """Whether a repo ``name`` under :attr:`owner` is in this scope by its globs.

        Case is ignored, as GitHub ignores it in repo names: ``exclude = ["payroll*"]``
        leaves out ``Payroll-api`` too. Names and globs are ASCII by their checks.
        """
        wanted = name.lower()
        return any(fnmatch.fnmatchcase(wanted, g.lower()) for g in self.match) and not any(
            fnmatch.fnmatchcase(wanted, g.lower()) for g in self.exclude
        )

    def summary(self) -> str:
        """One line for help: what the scope reaches."""
        parts = [f"every repo in {self.owner} matching {', '.join(self.match)}"]
        left_out = list(self.exclude)
        if not self.include_forks:
            left_out.append("forks")
        if not self.include_archived:
            left_out.append("archived repos")
        if left_out:
            parts.append("except " + ", ".join(left_out))
        return " ".join(parts)


MAX_BOARDS = 10
MAX_BOARD_NUMBER = 10**9
_BOARD = re.compile(r"(?P<owner>[^/]+)/(?P<number>[1-9][0-9]{0,9})")


class BoardPolicy(_Strict):
    """A GitHub Projects board a channel's issues may be added to (#309).

    Organisation boards only. The description is the team's own, from a reviewed
    jotflow.toml, and is what drafting chooses by: GitHub's goes stale.
    """

    board: str = Field(description="owner/number, as the board's URL shows it")
    nickname: Nickname = Field(description="Short name for the card, such as roadmap")
    description: str = Field(
        min_length=1,
        max_length=200,
        description="What belongs on the board; the agent picks by this",
    )

    @field_validator("board")
    @classmethod
    def _board(cls, value: str) -> str:
        match = _BOARD.fullmatch(value)
        if match is None or _OWNER.fullmatch(match["owner"]) is None:
            raise ValueError("a board is owner/number, such as example-org/3")
        if not 1 <= int(match["number"]) <= MAX_BOARD_NUMBER:
            raise ValueError("a board's number is from 1 to 1,000,000,000")
        return value

    @field_validator("description")
    @classmethod
    def _plain_description(cls, value: str) -> str:
        if problem := unsafe_text_problem(value):
            raise ValueError(f"description {problem}")
        return value

    @property
    def owner(self) -> str:
        return self.board.partition("/")[0]

    @property
    def number(self) -> int:
        return int(self.board.partition("/")[2])


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


class ChannelSummary(_Strict):
    """A weekly post listing the channel's ideas (#308). All three are needed."""

    day: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    time: str = Field(description="24-hour HH:MM, in the timezone below")
    timezone: str = Field(description="An IANA timezone, such as Europe/London")

    @field_validator("time")
    @classmethod
    def _clock(cls, value: str) -> str:
        if not _CLOCK.fullmatch(value):
            raise ValueError("time must be 24-hour HH:MM, such as 09:30")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}", value):
            raise ValueError("timezone must be an IANA name, such as Europe/London")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}; use an IANA name") from None
        return value

    @property
    def weekday(self) -> int:
        return WEEKDAYS.index(self.day)

    @property
    def hour_minute(self) -> tuple[int, int]:
        hour, minute = self.time.split(":")
        return int(hour), int(minute)


def title_excluded(title: str, globs: Iterable[str]) -> bool:
    """Whether a title glob leaves a board out, ignoring case (#309 PR 2)."""
    wanted = title.casefold()
    return any(fnmatch.fnmatchcase(wanted, glob.casefold()) for glob in globs)


def has_marker(description: str | None, marker: str) -> bool:
    """Whether ``description`` has ``marker`` as a whole word, ignoring case: so
    ``jotflow:triage-old`` is not ``jotflow:triage``."""
    return bool(description) and marker in str(description).casefold().split()


def is_board_marker(value: str) -> bool:
    return _BOARD_MARKER.fullmatch(value) is not None


MAX_BOARD_SCOPES = 3
MAX_SCOPE_NUMBERS = 10
MAX_CREATORS = 50
# A board's own marker, as a team writes it in the board's short description.
_BOARD_MARKER = re.compile(r"[a-z0-9][a-z0-9_-]{0,18}:[a-z0-9][a-z0-9_-]{0,19}")


def _title_glob(value: str) -> str:
    if not 1 <= len(value) <= 100 or unsafe_text_problem(value):
        raise ValueError("a title glob is 1 to 100 plain characters, such as '* (archive)'")
    return value


TitleGlob = Annotated[str, AfterValidator(_title_glob)]


class BoardCreator(_Strict):
    """A person whose boards a marker counts for (#348 review): pinned by GitHub
    account id, which survives a rename and can't be claimed by someone else. The
    login is a label to review it by, never what's matched."""

    login: str = Field(description="Their GitHub login, as a label")
    id: int = Field(
        ge=1,
        le=2**53,
        description="Their GitHub account id: the one matched. jotflow check prints it",
    )

    @field_validator("login")
    @classmethod
    def _login(cls, value: str) -> str:
        if not is_github_login(value) or value.endswith("[bot]"):
            raise ValueError(f"{shown(value)} is not a person's GitHub login")
        return value


class BoardScopePolicy(_Strict):
    """An organisation's boards a channel finds live, beside its listed ones (#309 PR 2).

    A board is in scope by its number, or by a marker in its short description
    that only the listed creators' boards count for. Titles never put a board in
    scope: anyone in the organisation can usually create and name a board.
    """

    owner: Owner = Field(description="The GitHub organisation, written out")
    numbers: tuple[int, ...] = Field(
        default=(),
        max_length=MAX_SCOPE_NUMBERS,
        description="Boards by number: the simplest and safest scope",
    )
    marker: str | None = Field(
        default=None,
        description="A word such as jotflow:triage in a board's short description",
    )
    creators: tuple[BoardCreator, ...] = Field(
        default=(),
        max_length=MAX_CREATORS,
        description=(
            "People whose boards the marker counts for, each as { login, id }: the id is "
            "matched, never the login. Required with marker"
        ),
    )
    exclude: tuple[TitleGlob, ...] = Field(
        default=(), max_length=MAX_GLOBS, description="Globs on board titles to leave out"
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional. The team's own words for the scope's boards, shown first",
    )
    aliases: dict[Nickname, int] = Field(
        default_factory=dict, description="Optional nicknames, mapped to board numbers"
    )

    @field_validator("numbers")
    @classmethod
    def _numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 1 <= n <= MAX_BOARD_NUMBER for n in value):
            raise ValueError("a board's number is from 1 to 1,000,000,000")
        if len(set(value)) != len(value):
            raise ValueError("a board number is listed more than once")
        return value

    @field_validator("marker")
    @classmethod
    def _marker(cls, value: str | None) -> str | None:
        if value is not None and not _BOARD_MARKER.fullmatch(value):
            raise ValueError("a marker is lowercase name:word, such as jotflow:triage")
        return value

    @field_validator("creators", mode="before")
    @classmethod
    def _pinned(cls, value: object) -> object:
        # A bare login is the old form, and has no id to match: refuse it, saying how.
        if isinstance(value, list | tuple):
            for item in value:
                if isinstance(item, str):
                    raise ValueError(
                        f"creator {shown(item)} needs its GitHub account id: write "
                        f'{{ login = "{item}", id = ... }}; jotflow check prints the id'
                    )
        return value

    @field_validator("creators")
    @classmethod
    def _unique(cls, value: tuple[BoardCreator, ...]) -> tuple[BoardCreator, ...]:
        if len({c.id for c in value}) != len(value):
            raise ValueError("a creator id is listed more than once")
        return value

    @field_validator("description")
    @classmethod
    def _plain_description(cls, value: str | None) -> str | None:
        if value is not None and (problem := unsafe_text_problem(value)):
            raise ValueError(f"description {problem}")
        return value

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not 1 <= n <= MAX_BOARD_NUMBER for n in value.values()):
            raise ValueError("an alias names a board number from 1 to 1,000,000,000")
        return value

    @model_validator(mode="after")
    def _numbers_or_marker(self) -> BoardScopePolicy:
        if not self.numbers and self.marker is None:
            raise ValueError("a board scope needs numbers or a marker; titles alone never count")
        if self.marker is not None and not self.creators:
            raise ValueError(
                "a marker needs creators: anyone in the organisation can write it into a board"
            )
        return self

    def excludes(self, title: str) -> bool:
        return title_excluded(title, self.exclude)

    def marked(self, description: str | None) -> bool:
        return self.marker is not None and has_marker(description, self.marker)


class ChannelPolicy(_Strict):
    """A Slack channel JotFlow watches and the repos it may feed."""

    repos: tuple[RepoPolicy, ...] = Field(default=())
    scopes: tuple[ScopePolicy, ...] = Field(
        default=(),
        max_length=MAX_SCOPES,
        description="Owners whose matching repos are found live, beside the listed repos (#236)",
    )
    boards: tuple[BoardPolicy, ...] = Field(
        default=(),
        max_length=MAX_BOARDS,
        description="GitHub Projects boards an issue may be added to, after approval (#309)",
    )
    board_scopes: tuple[BoardScopePolicy, ...] = Field(
        default=(),
        max_length=MAX_BOARD_SCOPES,
        description="Organisations whose boards are found live, by number or marker (#309)",
    )
    debounce_seconds: int | None = Field(
        default=None, ge=1, le=86_400, description="Overrides the top-level debounce window"
    )
    triage_guide: str | None = Field(
        default=None,
        max_length=MAX_TRIAGE_GUIDE,
        description=(
            "Optional. The team's guide to which repo, and which board, a request belongs on, "
            "in plain text. The drafting agent chooses by it, within the listed repos and "
            "boards (#317)"
        ),
    )
    attachments: Literal["off", "names", "text"] = Field(
        default="names",
        description=(
            "What drafting learns of a message's Slack files (#333): nothing, each file's "
            "name, type and size, or also a small text file's text, which a proposal may "
            "then copy into the issue. text needs the bot's files:read scope"
        ),
    )
    help: ChannelHelp = Field(
        default_factory=ChannelHelp, description="Overrides the top-level [help] settings"
    )
    questions: ChannelQuestions = Field(
        default_factory=ChannelQuestions, description="Overrides the top-level [questions]"
    )
    summary: ChannelSummary | None = Field(
        default=None, description="A weekly post of the channel's ideas (#308); off if unset"
    )

    @field_validator("triage_guide")
    @classmethod
    def _plain_triage_guide(cls, value: str | None) -> str | None:
        return _plain_policy_text("triage_guide", value, "write the guide")

    @model_validator(mode="after")
    def _unique_repos(self) -> ChannelPolicy:
        if not self.repos and not self.scopes:
            raise ValueError("a channel needs at least one repo or one scope")
        if dupes := _duplicates(r.repo for r in self.repos):
            raise ValueError(f"repos listed more than once: {', '.join(dupes)}")
        if dupes := _duplicates(s.owner for s in self.scopes):
            raise ValueError(f"scopes given more than once for: {', '.join(dupes)}")
        aliases = [alias for scope in self.scopes for alias in scope.aliases]
        aliases += [alias for scope in self.board_scopes for alias in scope.aliases]
        boards = [b.nickname for b in self.boards]
        if dupes := _duplicates([*(r.nickname for r in self.repos), *aliases, *boards]):
            raise ValueError(f"nicknames used more than once: {', '.join(dupes)}")
        if dupes := _duplicates(b.board for b in self.boards):
            raise ValueError(f"boards listed more than once: {', '.join(dupes)}")
        if dupes := _duplicates(s.owner.casefold() for s in self.board_scopes):
            raise ValueError(f"board scopes given more than once for: {', '.join(dupes)}")
        return self

    def board(self, ref: str) -> BoardPolicy | None:
        """A listed board by nickname or ``owner/number``, ignoring the owner's case."""
        wanted = ref.casefold()
        for board in self.boards:
            if wanted in (board.board.casefold(), board.nickname):
                return board
        return None

    def repo(self, ref: str, *, channel_id: str = "") -> RepoPolicy:
        """Find a repo by ``owner/name`` or nickname, ignoring case.

        Raises :class:`RepoNotAllowedError` when the channel does not list it.
        """
        wanted = ref.casefold()
        for repo in self.repos:
            if wanted in (repo.repo.casefold(), repo.nickname):
                return repo
        raise RepoNotAllowedError(channel_id, ref)

    def reaches(self, repo: str) -> bool:
        """Whether ``owner/name`` is listed, or a scope's globs cover it.

        Offline only: it can't tell a fork or an archived repo. For what may be
        written, ask the repo catalog.
        """
        name = repo.partition("/")[2]
        if any(r.repo.casefold() == repo.casefold() for r in self.repos):
            return True
        return any(under_owner(repo, s.owner) and s.covers(name) for s in self.scopes)


MAX_CONCURRENT_RUNS = 32


class AgentPolicy(_Strict):
    preset: PresetName | None = Field(
        default=None, description="Agent preset the runner launches; unset disables agent runs"
    )
    max_concurrent_runs: int = Field(
        default=2,
        ge=1,
        le=MAX_CONCURRENT_RUNS,
        description="How many agent runs may run at once, across all channels (D8)",
    )


class ApprovalPolicy(_Strict):
    """Who may approve (D7). Both options are off by default."""

    approvers: tuple[UserId, ...] = Field(
        default=(),
        description="Slack user ids; empty means anyone in the channel may approve",
    )
    require_second_approver: bool = Field(
        default=False,
        description=(
            "With approvers on, refuse a listed approver who wrote the thread's first message "
            "or a cited source, so every write needs a second person. Off by default (#244)"
        ),
    )
    reactions: bool = Field(default=False, description="Whether a reaction counts as approval")
    reaction: EmojiName = Field(
        default=DEFAULT_APPROVAL,
        description=(
            "The emoji name, without colons, that approves when reactions are on; "
            "ignored while they are off"
        ),
    )

    @property
    def approvers_enabled(self) -> bool:
        return bool(self.approvers)

    @field_validator("reaction")
    @classmethod
    def _emoji_name(cls, value: str) -> str:
        if value in KEYWORD_REACTIONS:
            raise ValueError(f"{value} already stands for a keyword; choose another emoji")
        if value in reserved():
            raise ValueError(
                f"JotFlow adds {value} itself to confirm keywords; choose another emoji"
            )
        return value

    @model_validator(mode="after")
    def _unique_approvers(self) -> ApprovalPolicy:
        if dupes := _duplicates(self.approvers):
            raise ValueError(f"approvers listed more than once: {', '.join(dupes)}")
        if self.require_second_approver and not self.approvers:
            # Without a list anyone may approve, the poster included: refuse a
            # setting a team could believe requires two people when it doesn't.
            raise ValueError(
                "require_second_approver needs [approval] approvers; list them, or remove "
                "require_second_approver"
            )
        return self


class IdentityPolicy(_Strict):
    """Who may remove someone else's GitHub link (#173), besides the approvers."""

    admins: tuple[UserId, ...] = Field(
        default=(),
        description="Slack user ids that may unlink anyone; approvers always may",
    )

    @model_validator(mode="after")
    def _unique_admins(self) -> IdentityPolicy:
        if dupes := _duplicates(self.admins):
            raise ValueError(f"admins listed more than once: {', '.join(dupes)}")
        return self


# GitHub's longest issue title.
MAX_ISSUE_TITLE = 256
MAX_TITLE_PREFIX = 32


class IssuesPolicy(_Strict):
    """What JotFlow adds to every new issue it files (#306). Both off by default.

    They're added when the issue is written, after the draft's ``title_pattern``
    check, so a person can't take them off on the card, and the card shows them.
    """

    label: Label | None = Field(
        default=None,
        description="A label added to every new issue; it must already exist in each repo",
    )
    title_prefix: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_TITLE_PREFIX,
        description="Text put before every new issue's title, such as '[Slack]'",
    )

    @field_validator("title_prefix")
    @classmethod
    def _plain_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip():
            raise ValueError("title_prefix must not start or end with a space")
        if problem := unsafe_text_problem(value):
            raise ValueError(f"title_prefix {problem}")
        if any(mark in value for mark in ("<", ">", "@", "`")):
            raise ValueError("title_prefix must be plain text, with no markup or mentions")
        return value

    def filed_title(self, title: str) -> str:
        """The title as filed: the prefix, a space, then the draft's title, within GitHub's
        limit. A title that already starts with the prefix isn't given it twice."""
        if not self.title_prefix or title.startswith(self.title_prefix + " "):
            return title
        return f"{self.title_prefix} {title}"[:MAX_ISSUE_TITLE].rstrip()

    def filed_labels(self, labels: Iterable[str]) -> list[str]:
        """The labels as filed: the draft's, then the JotFlow label if it isn't there."""
        kept = list(labels)
        if self.label and self.label.casefold() not in {label.casefold() for label in kept}:
            kept.append(self.label)
        return kept

    def snapshot(self) -> str:
        """These settings as canonical JSON, as a card or an approval records them."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_snapshot(cls, snapshot: str | None, current: IssuesPolicy) -> IssuesPolicy:
        """The settings a snapshot recorded, or ``current`` for one made before snapshots."""
        return current if snapshot is None else cls.model_validate_json(snapshot)

    def bare_title(self, title: str) -> str:
        """A filed title without the prefix, as ``jotflow rebuild`` restores a topic's."""
        if self.title_prefix and title.startswith(self.title_prefix + " "):
            return title[len(self.title_prefix) + 1 :]
        return title


class Policy(_Strict):
    """The whole of ``jotflow.toml``."""

    issue_links: IssueLinks = Field(
        default="permalinks",
        description="'none' keeps permalinks and every workspace detail out of issue bodies",
    )
    debounce_seconds: int = Field(
        default=120, ge=1, le=86_400, description="Quiet window before a thread is drafted"
    )
    issue_reaction: EmojiName | Literal[""] = Field(
        default=DEFAULT_ISSUE_REACTION,
        description=(
            "Reaction added to a proposal's source messages once its issue is written (#32); "
            "empty for none"
        ),
    )
    topic_window_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="Days since a message was last linked during which a topic stays active",
    )
    agent: AgentPolicy = Field(default_factory=AgentPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    help: HelpPolicy = Field(default_factory=HelpPolicy)
    questions: QuestionsPolicy = Field(default_factory=QuestionsPolicy)
    identity: IdentityPolicy = Field(default_factory=IdentityPolicy)
    issues: IssuesPolicy = Field(default_factory=IssuesPolicy)
    channels: dict[ChannelId, ChannelPolicy] = Field(min_length=1)

    def may_unlink_others(self, user_id: str) -> bool:
        """An approver or an identity admin may remove someone else's link (#173)."""
        return user_id in self.approval.approvers or user_id in self.identity.admins

    def channel(self, channel_id: str) -> ChannelPolicy:
        """Raise :class:`UnknownChannelError` for a channel the file does not list."""
        try:
            return self.channels[channel_id]
        except KeyError:
            raise UnknownChannelError(channel_id) from None

    def repo(self, channel_id: str, ref: str) -> RepoPolicy:
        """The repo ``ref`` names in ``channel_id``, by ``owner/name`` or nickname."""
        return self.channel(channel_id).repo(ref, channel_id=channel_id)

    @property
    def reads_docs(self) -> bool:
        """Whether any repo sets ``read_paths``, so drafting gets the doc tools (#209)."""
        return any(
            repo.read_paths is not None
            for channel in self.channels.values()
            for repo in channel.repos
        )

    @model_validator(mode="after")
    def _reactions_never_collide(self) -> Policy:
        """The issue reaction is JotFlow's own, so it can't also mean a keyword,
        JotFlow's keyword confirmation, or approval (#32, #154)."""
        name = self.issue_reaction
        if not name:
            return self
        if name in KEYWORD_REACTIONS:
            raise ValueError(f"issue_reaction {name} already stands for a keyword")
        if name in reserved():
            raise ValueError(f"issue_reaction {name} is JotFlow's keyword confirmation")
        if name == self.approval.reaction:
            raise ValueError(f"issue_reaction {name} is also the approval reaction")
        return self

    def debounce_for(self, channel_id: str) -> int:
        override = self.channel(channel_id).debounce_seconds
        return self.debounce_seconds if override is None else override

    def help_for(self, channel_id: str) -> HelpPolicy:
        """The channel's help settings, each one its own or the top-level default."""
        own = self.channel(channel_id).help.model_dump(exclude_none=True)
        return self.help.model_copy(update=own)

    def attachments_for(self, channel_id: str) -> str:
        """A channel's ``attachments`` setting (#333), or "off" for an unlisted one."""
        found = self.channels.get(channel_id)
        return found.attachments if found is not None else "off"

    def questions_for(self, channel_id: str) -> QuestionsPolicy:
        """The channel's question settings, each one its own or the top-level default."""
        own = self.channel(channel_id).questions.model_dump(exclude_none=True)
        return self.questions.model_copy(update=own)


class PolicyError(Exception):
    """``jotflow.toml`` is missing, unreadable, or invalid.

    ``problems`` holds one line per issue, each starting with the key it is about.
    """

    def __init__(self, path: Path | str, problems: list[str]) -> None:
        self.path = Path(path)
        self.problems = problems
        super().__init__(f"{self.path}:\n" + "\n".join(f"  {p}" for p in problems))


def _key_path(loc: tuple[int | str, ...]) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif item == "[key]":
            continue
        else:
            parts.append(f".{item}" if parts else item)
    return "".join(parts) or "(top level)"


def _describe(error: ValidationError) -> list[str]:
    problems: list[str] = []
    for detail in error.errors(include_url=False):
        key = _key_path(tuple(detail["loc"]))
        kind = detail["type"]
        if kind == "extra_forbidden":
            message = "unknown key"
        elif kind == "missing":
            message = "missing required key"
        else:
            message = str(detail["msg"]).removeprefix("Value error, ")
        problems.append(f"{key}: {message}")
    return problems


def parse_policy(text: str, *, path: Path | str = "jotflow.toml") -> Policy:
    """Parse and validate policy from TOML text. ``path`` is used in messages."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(path, [f"not valid TOML: {exc}"]) from None
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(path, _describe(exc)) from None


def load_policy(path: Path | str) -> Policy:
    """Read ``jotflow.toml`` from ``path``. Raises :class:`PolicyError`."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PolicyError(file, ["file not found; run `jotflow init` to create it"]) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(file, [f"cannot read file: {exc}"]) from None
    return parse_policy(text, path=file)
