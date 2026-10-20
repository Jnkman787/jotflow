"""A repo's ``title_pattern``, matched under a hard time limit (#216).

The pattern is the admin's, but the titles it checks are agent-written, and so
shaped by Slack and GitHub text. A pattern that backtracks badly could stall a
drafting run, and no static analysis reliably rules that out. So the guarantee
is a time limit on the match itself: every match runs in the third-party
``regex`` engine with ``timeout=``, off the event loop, with ``concurrent=True``
so it releases the GIL. A match that runs out of time counts as a refusal,
never as a match.

A cheap syntax filter still runs first, when the policy loads:

- 1 to 200 characters, and a regular expression both ``re`` and ``regex``
  accept, so the admin finds out at load time
- none of the syntax the two engines read differently: an unescaped ``{`` that
  isn't a numeric repeat, which ``regex`` reads as fuzzy matching, and ``[:`` or
  ``[[``, which it reads as a POSIX class or a nested set
- no backreferences, lookarounds, or conditionals
- at most three repeats that can match more than once
"""

from __future__ import annotations

import asyncio
import importlib
import re
from enum import StrEnum
from typing import Any

import regex

# The standard library's own parser, so the filter sees what re compiles. It is
# private and has no type stubs, so it is loaded untyped.
parser: Any = importlib.import_module("re._parser")

MAX_PATTERN = 200
MAX_TITLE = 256
MAX_REPEATS = 3
# How long one match may take, in seconds. Real title formats take well under 1 ms.
MATCH_TIMEOUT = 0.05

# A numeric repeat, the only thing an unescaped { may start: {3}, {2,}, {,4}, {1,5}.
_NUMERIC_REPEAT = re.compile(r"\{(?:\d+(?:,\d*)?|,\d+)\}")

_REPEATS = {parser.MAX_REPEAT, parser.MIN_REPEAT, parser.POSSESSIVE_REPEAT}
_LEAVES = {
    parser.LITERAL,
    parser.NOT_LITERAL,
    parser.ANY,
    parser.IN,
    parser.CATEGORY,
    parser.AT,
}
# Titles nothing in a real format should take long on, for jotflow check's probe.
HOSTILE_TITLES = ("a" * MAX_TITLE, "a" * (MAX_TITLE - 1) + "!", "ab" * (MAX_TITLE // 2))


class Match(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    TIMEOUT = "timeout"


def _walk(items: Any, repeats: list[int]) -> None:
    for op, value in items:
        if op in _REPEATS:
            _low, high, body = value
            if high is parser.MAXREPEAT or high > 1:
                repeats.append(1)
            _walk(body, repeats)
        elif op is parser.SUBPATTERN:
            _walk(value[-1], repeats)
        elif op is parser.ATOMIC_GROUP:
            _walk(value, repeats)
        elif op is parser.BRANCH:
            for branch in value[1]:
                _walk(branch, repeats)
        elif op in (parser.ASSERT, parser.ASSERT_NOT):
            raise ValueError("lookarounds are not allowed")
        elif op in (parser.GROUPREF, parser.GROUPREF_EXISTS):
            raise ValueError("backreferences and conditionals are not allowed")
        elif op not in _LEAVES:
            raise ValueError(f"{str(op).lower()} is not allowed")


def _engine_differences(pattern: str) -> None:
    """Refuse syntax ``re`` and ``regex`` read differently, so both match the same."""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "{" and not _NUMERIC_REPEAT.match(pattern, index):
            raise ValueError(
                "write \\{ for a literal brace; { may only start a repeat such as {1,5}"
            )
        if char == "[" and pattern.startswith(("[:", "[["), index):
            raise ValueError("POSIX classes such as [:alpha:] and nested sets are not allowed")
        index += 1


def check_title_pattern(pattern: str) -> str:
    """``pattern`` if the cheap syntax filter allows it, else ``ValueError``."""
    if not pattern or len(pattern) > MAX_PATTERN:
        raise ValueError(f"title_pattern must be 1 to {MAX_PATTERN} characters")
    _engine_differences(pattern)
    try:
        parsed = parser.parse(pattern)
        regex.compile(pattern)
    except (re.error, regex.error) as exc:
        raise ValueError(f"title_pattern is not a regular expression: {exc}") from None
    repeats: list[int] = []
    _walk(parsed, repeats)
    if len(repeats) > MAX_REPEATS:
        raise ValueError(f"title_pattern may have at most {MAX_REPEATS} *, + or {{n,m}} repeats")
    return pattern


def match_title(pattern: str, title: str) -> Match:
    """Whether ``title`` fully matches ``pattern``, within MATCH_TIMEOUT seconds."""
    try:
        found = regex.fullmatch(pattern, title, timeout=MATCH_TIMEOUT, concurrent=True)
    except TimeoutError:
        return Match.TIMEOUT
    return Match.MATCH if found is not None else Match.MISMATCH


async def match_title_async(pattern: str, title: str) -> Match:
    """:func:`match_title` in a worker thread, so the event loop keeps running."""
    return await asyncio.to_thread(match_title, pattern, title)


def times_out(pattern: str) -> bool:
    """Whether ``pattern`` runs out of time on a hostile title, for ``jotflow check``."""
    return any(match_title(pattern, title) is Match.TIMEOUT for title in HOSTILE_TITLES)
