"""A repo's title_pattern: only patterns that always match quickly (#216)."""

from __future__ import annotations

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
