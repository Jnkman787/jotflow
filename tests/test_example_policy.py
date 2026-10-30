"""The example jotflow.toml parses, and so does every example line it comments out (#371).

A commented example that fails once uncommented, in the wrong table or with a value
the policy refuses, is exactly what a reader copies. So each one is uncommented here
and the whole file parsed, and CI fails on a misplaced or invalid example.
"""

from __future__ import annotations

import re
from pathlib import Path

from jotflow.policy import parse_policy

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "jotflow.example.toml"
# A commented TOML line: a table header, or key = value. Prose never has that shape.
_COMMENTED = re.compile(r"^# ((\[{1,2}[A-Za-z0-9_.<>-]+\]{1,2})|([A-Za-z_][A-Za-z0-9_-]* = .+))$")


def uncommented(text: str) -> tuple[str, int]:
    lines, count = [], 0
    for line in text.splitlines():
        match = _COMMENTED.match(line)
        if match:
            lines.append(match.group(1))
            count += 1
        else:
            lines.append(line)
    return "\n".join(lines) + "\n", count


def test_the_example_parses_as_committed() -> None:
    assert parse_policy(EXAMPLE.read_text(encoding="utf-8"))


def test_every_commented_example_parses_once_uncommented() -> None:
    text, count = uncommented(EXAMPLE.read_text(encoding="utf-8"))
    assert count >= 15  # the example's commented options, so the pattern still finds them
    policy = parse_policy(text)
    web = policy.channels["C0EXAMPLE1"].repos[0]
    assert web.aliases == ("shop",) and web.owner_hints and web.read_paths
    assert policy.channels["C0EXAMPLE1"].priority_labels
    assert policy.channels["C0EXAMPLE1"].summary is not None
    assert policy.identity.github_org and policy.issues.label


def test_the_pattern_leaves_prose_alone() -> None:
    prose = "# labels is optional. Without it, a proposal may use any label\n"
    assert uncommented(prose) == (prose, 0)
