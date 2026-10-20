"""A repo's title_pattern: only patterns that always match quickly (#216)."""

from __future__ import annotations

import random
import time

import pytest

from jotflow.title_pattern import MAX_TITLE, check_title_pattern, title_matches


@pytest.mark.parametrize(
    "pattern",
    [
        r"^(feat|fix|docs)(\([a-z-]+\))?: .+$",
        r"\[[A-Z]+-\d+\] .+",
        r"(Bug|Feature): .{3,80}",
        r"[a-z]+",
    ],
)
def test_real_title_formats_are_allowed(pattern: str) -> None:
    assert check_title_pattern(pattern) == pattern


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        (r"(a+)+", "a group may take only ?"),
        (r"(a|a)*", "a group may take only ?"),
        (r"(a|ab)*", "a group may take only ?"),
        (r"(\w+\s?)*", "a group may take only ?"),
        (r"(?:ab){2,}", "a group may take only ?"),
        (r"(?=x).+", "lookarounds"),
        (r"(?<!x).+", "lookarounds"),
        (r"(a)\1", "backreferences"),
        (r"(a)?(?(1)b|c)", "backreferences and conditionals"),
        (r".*.*.*.*", "at most 3"),
        # Choices with no unbounded repeat are still exponential when chained (#216).
        ("a?" * 24 + "a" * 24, "at most 8 choices"),
        ("(a|aa)?" * 17 + "b", "at most 8 choices"),
        ("a?" * 9, "at most 8 choices"),
        ("", "1 to 200 characters"),
        ("x" * 201, "1 to 200 characters"),
        ("(", "not a regular expression"),
    ],
)
def test_patterns_that_could_be_slow_are_refused(pattern: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        check_title_pattern(pattern)


@pytest.mark.parametrize("pattern", [r".*.*.*!", r"[a-z ]*[a-z ]*[a-z ]*!", r"(x)?.+.+.+!"])
def test_the_slowest_allowed_patterns_finish_fast_on_a_hostile_title(pattern: str) -> None:
    check_title_pattern(pattern)
    hostile = "a" * MAX_TITLE  # no "!", so every split is tried
    start = time.perf_counter()
    assert not title_matches(pattern, hostile)
    assert time.perf_counter() - start < 0.5


def test_a_title_must_match_the_whole_pattern() -> None:
    pattern = r"(feat|fix): .+"
    assert title_matches(pattern, "feat: add CSV export")
    assert not title_matches(pattern, "Add CSV export")
    assert not title_matches(pattern, "a feat: inside")


def test_a_long_conventional_commits_list_is_allowed() -> None:
    types = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
    check_title_pattern(rf"^({types})(\([a-z-]+\))?!?: .+$")


_ATOMS = ["a", "b", ".", "[ab]", r"\w", "(a|aa)", "(a|b)", "(ab|a)", "(aa|a)", "(a|aa|aaa)"]
_QUANTS = ["", "", "?", "*", "+", "{0,3}", "{1,5}", "{0,9}"]
_HOSTILE = ["a" * 256, "a" * 255 + "!", "ab" * 128, "aab" * 85 + "a", "a" * 128 + "b" * 128]


def _piece(rng: random.Random, depth: int) -> str:
    if depth < 2 and rng.random() < 0.25:
        inner = "".join(_piece(rng, depth + 1) for _ in range(rng.randint(1, 4)))
        return f"({inner})" + rng.choice(["", "?"])
    return rng.choice(_ATOMS) + rng.choice(_QUANTS)


def test_every_allowed_pattern_in_a_fuzz_matches_hostile_titles_fast() -> None:
    # Random patterns from a small grammar; those the validator allows must stay
    # fast on titles built to make them backtrack. 9,000 tried by hand: worst 27 ms.
    rng = random.Random(216)
    allowed = 0
    while allowed < 200:
        pattern = "".join(_piece(rng, 0) for _ in range(rng.randint(1, 10)))
        pattern += rng.choice(["", "!", "$", "b"])
        try:
            check_title_pattern(pattern)
        except ValueError:
            continue
        allowed += 1
        for title in _HOSTILE:
            start = time.perf_counter()
            title_matches(pattern, title)
            assert time.perf_counter() - start < 0.5, pattern
