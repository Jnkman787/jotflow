"""Intent keywords at the start of a message (D10).

A keyword is the first word of a message followed by a colon, so "Skip button
is broken" is ordinary text and "skip: not an idea" is the ``skip`` keyword.
Case does not matter. This module owns that rule, so the work packet (#20),
the keyword handler (#56), and replay can never disagree about what counts as
a keyword.

Typos are tolerated only on keywords of four or more letters, ``idea`` and
``skip``, and only one kind: two neighbouring letters swapped, as in ``skpi:``
or ``idae:``. That is the common keyboard slip, and no English word is one
swap away from either keyword. A wrong letter is not tolerated, because
``Ship:``, ``Skin:``, and ``Slip:`` are ordinary words that would silently keep
a thread out of drafting. A longer or shorter word, such as ``ideas:``, is
ordinary text too, and ``re:`` and ``bug:`` must be exact.

``re:`` takes an issue reference, resolved against the channel's repos (D11):
``42`` when the channel has one repo, ``web#42`` or ``owner/name#42`` otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from jotflow.policy import ChannelPolicy, RepoPolicy

Keyword = Literal["idea", "bug", "re", "skip"]
KEYWORDS: tuple[Keyword, ...] = ("idea", "bug", "re", "skip")
# Keywords long enough to tolerate a typo (D10).
TYPO_TOLERANT: tuple[Keyword, ...] = ("idea", "skip")

# Reactions that act like a keyword. re: has none: it needs an issue number.
REACTIONS: dict[str, Keyword] = {"no_entry_sign": "skip"}

# Leading spaces, one word, a colon, then optional spaces. The colon is required.
_PREFIX = re.compile(r"\s*([A-Za-z]+):[ \t]*")
_REFERENCE = re.compile(r"(?:(?P<repo>[A-Za-z0-9][\w./-]*)#)?(?P<number>[0-9]+)")


def _one_swap(word: str, keyword: str) -> bool:
    """``keyword`` with two neighbouring letters swapped, and nothing else changed."""
    if len(word) != len(keyword):
        return False
    diffs = [i for i, (a, b) in enumerate(zip(word, keyword, strict=True)) if a != b]
    if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
        i, j = diffs
        return word[i] == keyword[j] and word[j] == keyword[i]
    return False


def _keyword_for(word: str) -> Keyword | None:
    for keyword in KEYWORDS:
        if word == keyword:
            return keyword
    for keyword in TYPO_TOLERANT:
        if _one_swap(word, keyword):
            return keyword
    return None


def split_keyword(text: str) -> tuple[Keyword | None, str]:
    """The message's keyword, if it starts with one, and the text without it."""
    match = _PREFIX.match(text)
    if match is None:
        return None, text
    keyword = _keyword_for(match.group(1).lower())
    if keyword is None:
        return None, text
    return keyword, text[match.end() :]


# A keyword word, then punctuation where the colon belongs, such as "Idea - dark mode".
# A plain hyphen counts only with a space beside it, so "re-run" is a word.
# Commas and full stops are left out: "Idea, maybe..." and "Bug. The login..." are prose.
_WRONG_MARK = re.compile(r"\s*([A-Za-z]+)(?:\s*[\u2013\u2014;=]|\s+-|-\s)")
# "re 42", "re #42", or "re web#42": an issue reference without the colon.
_BARE_RE = re.compile(r"\s*re\s+#?(?:[A-Za-z0-9][\w./-]*#)?[0-9]+\b", re.IGNORECASE)


def near_miss(text: str) -> Keyword | None:
    """The keyword a message seems to mean but did not type correctly, if any.

    JotFlow never acts on a near miss: it only suggests the right form. To
    stay quiet, only clear mistakes count: the keyword alone, the keyword with
    a dash, semicolon, or equals sign where the colon goes, or ``re`` before an issue number.
    A keyword followed by an ordinary word, as in "Skip button is broken",
    is ordinary text.
    """
    if split_keyword(text)[0] is not None:
        return None
    alone = _keyword_for(text.strip().rstrip("!.?").lower())
    if alone is not None:
        return alone
    if _BARE_RE.match(text):
        return "re"
    mark = _WRONG_MARK.match(text)
    if mark is not None:
        return _keyword_for(mark.group(1).lower())
    return None


class ReferenceError(ValueError):
    """An ``re:`` reference JotFlow cannot use. The message says why."""


@dataclass(frozen=True)
class IssueReference:
    """The issue an ``re:`` message points at, in one of the channel's repos."""

    repo: RepoPolicy
    number: int

    @property
    def handle(self) -> str:
        """The short form people type, such as ``web#42``."""
        return f"{self.repo.nickname}#{self.number}"


def parse_reference(text: str, channel: ChannelPolicy) -> IssueReference:
    """Resolve the start of an ``re:`` message's text to an issue in ``channel``."""
    words = text.split(maxsplit=1)
    match = _REFERENCE.fullmatch(words[0].rstrip(".,:;")) if words else None
    if match is None:
        raise ReferenceError(
            "re: needs an issue, such as re: 42 or re: web#42, before anything else"
        )
    number = int(match["number"])
    if number <= 0:
        raise ReferenceError("an issue number starts at 1")
    name = match["repo"]
    if name is None:
        if len(channel.repos) > 1:
            handles = ", ".join(f"{r.nickname}#{number}" for r in channel.repos)
            raise ReferenceError(f"this channel feeds several repos; say which, such as {handles}")
        return IssueReference(channel.repos[0], number)
    wanted = name.casefold()
    for repo in channel.repos:
        if wanted in (repo.nickname, repo.repo.casefold()):
            return IssueReference(repo, number)
    nicknames = ", ".join(r.nickname for r in channel.repos)
    raise ReferenceError(f"{name} is not one of this channel's repos ({nicknames})")
