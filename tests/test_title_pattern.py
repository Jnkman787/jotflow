"""A repo's title_pattern, matched under a hard time limit (#216)."""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from jotflow import title_pattern
from jotflow.title_pattern import (
    HOSTILE_TITLES,
    MATCH_TIMEOUT,
    Match,
    check_title_pattern,
    match_title,
    match_title_async,
    times_out,
)

CONVENTIONAL = r"^(feat|fix|docs)(\([a-z-]+\))?: .+$"


def _alternation(width: int) -> str:
    return "(" + "|".join("a" * n for n in range(1, width + 1)) + ")"


# Every shape security and review found slow with re, each within 200 characters.
SLOW = {
    "three repeats and wide alternations": "a*a*a*" + _alternation(6) * 5 + "b",
    "dot-stars between alternations": ".*" + (_alternation(8) + ".*") * 2 + _alternation(8) + "!",
    "class repeats and optional pairs": "[ab]*[ab]*[ab]*" + "(a|b)?" * 5 + "c",
    "eight ten-way alternations": "(a|a|a|a|a|a|a|a|a|a)" * 8 + "b",
    "optional runs at the maximum": "a?" * 66 + "a" * 68,
    "optional alternations at the maximum": "(a|aa)?" * 28 + "bbbb",
}
# About 100 ms end to end: the limit plus thread and timer overhead.
WITHIN = MATCH_TIMEOUT + 0.07


@pytest.mark.parametrize(
    "pattern",
    [
        CONVENTIONAL,
        r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z-]+\))?!?: .+$",
        r"\[[A-Z]+-\d+\] .+",
        r"(Bug|Feature): .{3,80}",
        r".*.*.*!",
        r"\{[A-Z]+\} .{3,80}",
    ],
)
def test_real_title_formats_are_allowed(pattern: str) -> None:
    assert check_title_pattern(pattern) == pattern


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        (r"(?=x).+", "lookarounds"),
        (r"(?<!x).+", "lookarounds"),
        (r"(a)\1", "backreferences"),
        (r"(a)?(?(1)b|c)", "backreferences and conditionals"),
        (r".*.*.*.*", "at most 3"),
        (r"(a+)+(b+)+", "at most 3"),
        (r"(?:cat){e<=1}", "literal brace"),  # regex reads this as fuzzy matching
        (r"[[:alpha:]]+", "POSIX classes"),
        (r"[[ab]]+", "nested sets"),
        (r"a{x}", "literal brace"),
        ("", "1 to 200 characters"),
        ("x" * 201, "1 to 200 characters"),
        ("(", "not a regular expression"),
    ],
)
def test_the_syntax_filter_refuses(pattern: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        check_title_pattern(pattern)


@pytest.mark.parametrize("pattern", SLOW.values(), ids=SLOW.keys())
def test_every_slow_shape_returns_refused_within_the_limit(pattern: str) -> None:
    check_title_pattern(pattern)  # the filter lets them through; the limit is the guarantee
    for title in HOSTILE_TITLES:
        start = time.perf_counter()
        result = match_title(pattern, title)
        assert time.perf_counter() - start < WITHIN, (pattern, len(title))
        if title == "a" * 256:
            # None of these can match 256 a's, so it is a mismatch or a timeout.
            assert result is not Match.MATCH


@pytest.mark.parametrize("pattern", SLOW.values(), ids=SLOW.keys())
async def test_every_slow_shape_returns_within_the_limit_end_to_end(pattern: str) -> None:
    # Through the worker thread, as propose_issue calls it.
    start = time.perf_counter()
    result = await match_title_async(pattern, "a" * 256)
    assert time.perf_counter() - start < WITHIN
    assert result is not Match.MATCH


def test_a_match_that_runs_out_of_time_is_a_timeout_never_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(title_pattern, "MATCH_TIMEOUT", 0.001)
    assert match_title(SLOW["three repeats and wide alternations"], "a" * 256) is Match.TIMEOUT


def test_dot_stars_stay_quick_and_a_conventional_title_matches() -> None:
    for title in HOSTILE_TITLES:
        start = time.perf_counter()
        assert match_title(r".*.*.*!", title) is not Match.TIMEOUT
        assert time.perf_counter() - start < MATCH_TIMEOUT
    assert match_title(CONVENTIONAL, "feat(export): add a CSV export") is Match.MATCH
    assert match_title(CONVENTIONAL, "Add a CSV export") is Match.MISMATCH


async def test_a_slow_match_leaves_the_event_loop_running() -> None:
    # regex releases the GIL with concurrent=True, so other tasks run meanwhile.
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.002)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await match_title_async(SLOW["three repeats and wide alternations"], "a" * 256)
    finally:
        ticker.cancel()
    assert ticks >= 3


def test_check_names_a_pattern_that_runs_out_of_time() -> None:
    assert times_out(SLOW["three repeats and wide alternations"])
    assert not times_out(CONVENTIONAL)


_ATOMS = ["a", "b", ".", "[ab]", r"\w", "(a|aa)", "(a|b)", _alternation(4), _alternation(8)]
_QUANTS = ["", "", "?", "*", "+", "{0,3}", "{1,5}"]


def _piece(rng: random.Random, depth: int) -> str:
    if depth < 2 and rng.random() < 0.25:
        inner = "".join(_piece(rng, depth + 1) for _ in range(rng.randint(1, 4)))
        return f"({inner})" + rng.choice(["", "?", "*"])
    return rng.choice(_ATOMS) + rng.choice(_QUANTS)


def test_every_allowed_pattern_in_a_fuzz_returns_within_the_limit() -> None:
    # Random patterns, wide alternations and repeated groups included: whatever the
    # filter lets through, a match on a hostile title never runs past the limit.
    rng = random.Random(216)
    allowed = 0
    while allowed < 60:
        pattern = "".join(_piece(rng, 0) for _ in range(rng.randint(1, 10)))
        pattern += rng.choice(["", "!", "$", "b"])
        try:
            check_title_pattern(pattern)
        except ValueError:
            continue
        allowed += 1
        for title in HOSTILE_TITLES[:2]:
            start = time.perf_counter()
            match_title(pattern, title)
            assert time.perf_counter() - start < WITHIN, pattern
