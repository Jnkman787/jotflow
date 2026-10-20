"""A repo's ``title_pattern``, allowed only if it can always match cheaply (#216).

The pattern is the admin's, but the titles it checks are agent-written, and so
shaped by Slack and GitHub text. Python's ``re`` can't be interrupted, so a
pattern that backtracks badly could hang a drafting run. Rather than refuse the
known-bad shapes, this allows only shapes that can't blow up:

- ``*``, ``+`` and ``{n,m}`` apply only to one character, a class, or ``.``
- a group takes ``?`` at most, so alternation never repeats
- no backreferences, lookarounds, or conditionals
- at most eight choice points: every quantifier that can match a varying
  number of times, ``?`` included, and every alternation
- of those, at most three repeats that can match more than once

A run of choices, such as ``a?`` many times over, is exponential even when
nothing repeats unboundedly, so the choice budget is what keeps the worst case
small for a title of at most 256 characters. A test fuzzes allowed patterns
against hostile titles to hold that (#216).

That covers real title formats, such as ``^(feat|fix|docs)(\\([a-z-]+\\))?: .+$``.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

# The standard library's own parser, so the check sees exactly what re compiles.
# It is private and has no type stubs, so it is loaded untyped.
parser: Any = importlib.import_module("re._parser")

MAX_PATTERN = 200
MAX_TITLE = 256
MAX_REPEATS = 3
MAX_CHOICES = 8

_SINGLE = {parser.LITERAL, parser.NOT_LITERAL, parser.ANY, parser.IN, parser.CATEGORY}
_REPEATS = {parser.MAX_REPEAT, parser.MIN_REPEAT, parser.POSSESSIVE_REPEAT}


class _Budget:
    """Choice points in a pattern: where matching can try more than one way."""

    def __init__(self) -> None:
        self.choices = 0
        self.repeats = 0


def _walk(items: Any, budget: _Budget) -> None:
    for op, value in items:
        if op in _REPEATS:
            low, high, body = value
            if low != high:
                budget.choices += 1
            if len(body) == 1 and body[0][0] in _SINGLE:
                if high is parser.MAXREPEAT or high > 1:
                    budget.repeats += 1
                continue
            if (low, high) != (0, 1):
                raise ValueError("a group may take only ?, never *, + or {n,m}")
            _walk(body, budget)
        elif op is parser.SUBPATTERN:
            _walk(value[-1], budget)
        elif op is parser.ATOMIC_GROUP:
            _walk(value, budget)
        elif op is parser.BRANCH:
            budget.choices += 1
            for branch in value[1]:
                _walk(branch, budget)
        elif op in (parser.ASSERT, parser.ASSERT_NOT):
            raise ValueError("lookarounds are not allowed")
        elif op in (parser.GROUPREF, parser.GROUPREF_EXISTS):
            raise ValueError("backreferences and conditionals are not allowed")
        elif op in _SINGLE or op is parser.AT:
            continue
        else:
            raise ValueError(f"{str(op).lower()} is not allowed")


def check_title_pattern(pattern: str) -> str:
    """``pattern`` if it is a regular expression this module allows, else ``ValueError``."""
    if not pattern or len(pattern) > MAX_PATTERN:
        raise ValueError(f"title_pattern must be 1 to {MAX_PATTERN} characters")
    try:
        re.compile(pattern)
        parsed = parser.parse(pattern)
    except re.error as exc:
        raise ValueError(f"title_pattern is not a regular expression: {exc}") from None
    budget = _Budget()
    _walk(parsed, budget)
    if budget.repeats > MAX_REPEATS:
        raise ValueError(
            f"title_pattern may repeat at most {MAX_REPEATS} characters or classes with "
            "*, + or {n,m}, so it always matches quickly"
        )
    if budget.choices > MAX_CHOICES:
        raise ValueError(
            f"title_pattern may have at most {MAX_CHOICES} choices (each ?, *, +, {{n,m}} "
            "or | group), so it always matches quickly"
        )
    return pattern


def title_matches(pattern: str, title: str) -> bool:
    """Whether ``title`` matches the whole of ``pattern``."""
    return re.fullmatch(pattern, title[:MAX_TITLE]) is not None
