"""Admin policy, loaded from ``jotflow.toml``.

This is the policy tier of D2: values an admin owns and changes by editing a
file deployed with the service. Day-to-day state, such as channel mode and
identity links, lives in the database instead.

Every key is checked. An unknown or misspelt key is an error, and every error
names the key it is about, for example ``channels.C0EXAMPLE1.repos[0].nickname``.
See ``jotflow.example.toml`` for a complete file.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from jotflow.models import RepoName, shown

_CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{2,}")
_USER_ID = re.compile(r"[UW][A-Z0-9]{2,}")
_NICKNAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
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
MAX_LABEL_LENGTH = 50  # GitHub's limit on a label name

# Bidirectional overrides and isolates, which can make text on a card read
# differently from what it is. Other format characters stay allowed: emoji
# sequences rely on the zero-width joiner.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def label_problem(label: str) -> str | None:
    """Why ``label`` cannot be a label name, or None if it can.

    Labels reach the Slack card and GitHub. With no label list they come from
    the agent, so control characters, line breaks, bidirectional overrides,
    and surrounding spaces are refused here rather than trusted downstream.
    """
    if not 0 < len(label) <= MAX_LABEL_LENGTH:
        return f"must be 1 to {MAX_LABEL_LENGTH} characters"
    if label != label.strip():
        return "must not start or end with a space"
    for char in label:
        if unicodedata.category(char) in {"Cc", "Zl", "Zp"} or char in _BIDI_CONTROLS:
            return "must not contain control characters or line breaks"
    return None


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


class RepoPolicy(_Strict):
    """One repo a channel may feed (D11)."""

    repo: RepoName = Field(description="owner/name")
    nickname: Nickname = Field(description="Short name people use, as in `re: web#42`")
    description: str = Field(
        min_length=1, max_length=200, description="What the repo covers; the agent picks by this"
    )
    labels: tuple[Label, ...] = Field(
        default=(),
        description=(
            "Optional. Narrows the labels a proposal may set to this list. Missing or empty "
            "means any label that already exists on the repo"
        ),
    )

    @model_validator(mode="after")
    def _unique_labels(self) -> RepoPolicy:
        if dupes := _duplicates(self.labels):
            raise ValueError(f"labels listed more than once: {', '.join(dupes)}")
        return self

    @property
    def restricts_labels(self) -> bool:
        """Whether a label list narrows what proposals may set."""
        return bool(self.labels)

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
        allowed = {label.casefold(): label for label in self.labels}
        rejected = [label for label in wanted if label.casefold() not in allowed]
        if rejected:
            raise LabelNotAllowedError(self.repo, rejected)
        canonical: list[str] = []
        for label in wanted:
            if allowed[label.casefold()] not in canonical:
                canonical.append(allowed[label.casefold()])
        return canonical


class ChannelPolicy(_Strict):
    """A Slack channel JotFlow watches and the repos it may feed."""

    repos: tuple[RepoPolicy, ...] = Field(min_length=1)
    debounce_seconds: int | None = Field(
        default=None, ge=1, le=86_400, description="Overrides the top-level debounce window"
    )

    @model_validator(mode="after")
    def _unique_repos(self) -> ChannelPolicy:
        if dupes := _duplicates(r.repo for r in self.repos):
            raise ValueError(f"repos listed more than once: {', '.join(dupes)}")
        if dupes := _duplicates(r.nickname for r in self.repos):
            raise ValueError(f"nicknames used more than once: {', '.join(dupes)}")
        return self

    def repo(self, ref: str, *, channel_id: str = "") -> RepoPolicy:
        """Find a repo by ``owner/name`` or nickname, ignoring case.

        Raises :class:`RepoNotAllowedError` when the channel does not list it.
        """
        wanted = ref.casefold()
        for repo in self.repos:
            if wanted in (repo.repo.casefold(), repo.nickname):
                return repo
        raise RepoNotAllowedError(channel_id, ref)


class AgentPolicy(_Strict):
    preset: PresetName | None = Field(
        default=None, description="Agent preset the runner launches; unset disables agent runs"
    )


class ApprovalPolicy(_Strict):
    """Who may approve (D7). Both options are off by default."""

    approvers: tuple[UserId, ...] = Field(
        default=(),
        description="Slack user ids; empty means anyone in the channel may approve",
    )
    reactions: bool = Field(default=False, description="Whether a reaction counts as approval")

    @property
    def approvers_enabled(self) -> bool:
        return bool(self.approvers)

    @model_validator(mode="after")
    def _unique_approvers(self) -> ApprovalPolicy:
        if dupes := _duplicates(self.approvers):
            raise ValueError(f"approvers listed more than once: {', '.join(dupes)}")
        return self


class Policy(_Strict):
    """The whole of ``jotflow.toml``."""

    issue_links: IssueLinks = Field(
        default="permalinks",
        description="'none' keeps permalinks and every workspace detail out of issue bodies",
    )
    debounce_seconds: int = Field(
        default=120, ge=1, le=86_400, description="Quiet window before a thread is drafted"
    )
    agent: AgentPolicy = Field(default_factory=AgentPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    channels: dict[ChannelId, ChannelPolicy] = Field(min_length=1)

    def channel(self, channel_id: str) -> ChannelPolicy:
        """Raise :class:`UnknownChannelError` for a channel the file does not list."""
        try:
            return self.channels[channel_id]
        except KeyError:
            raise UnknownChannelError(channel_id) from None

    def repo(self, channel_id: str, ref: str) -> RepoPolicy:
        """The repo ``ref`` names in ``channel_id``, by ``owner/name`` or nickname."""
        return self.channel(channel_id).repo(ref, channel_id=channel_id)

    def debounce_for(self, channel_id: str) -> int:
        override = self.channel(channel_id).debounce_seconds
        return self.debounce_seconds if override is None else override


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
