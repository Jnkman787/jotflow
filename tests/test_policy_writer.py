from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from jotflow.policy import Policy, load_policy, parse_policy
from jotflow.policy_writer import render_policy, toml_string

EXAMPLE = Path(__file__).resolve().parents[1] / "jotflow.example.toml"


def _policy(**overrides: object) -> Policy:
    data: dict[str, object] = {
        "channels": {
            "C0ONE": {"repos": [{"repo": "acme/web", "nickname": "web", "description": "Web"}]}
        }
    }
    data.update(overrides)
    return Policy.model_validate(data)


def test_example_round_trips() -> None:
    policy = load_policy(EXAMPLE)
    assert parse_policy(render_policy(policy)) == policy


def test_minimal_policy_round_trips_without_a_preset() -> None:
    policy = _policy()
    text = render_policy(policy)
    assert parse_policy(text) == policy
    assert '# preset = "claude-code"' in text


def test_every_setting_round_trips() -> None:
    policy = _policy(
        issue_links="none",
        debounce_seconds=30,
        agent={"preset": "claude-code"},
        approval={"approvers": ["U0AAA", "U0BBB"], "reactions": True},
        channels={
            "G0PRIV": {
                "debounce_seconds": 5,
                "repos": [
                    {
                        "repo": "acme/api",
                        "nickname": "api",
                        "description": "API",
                        "labels": ["bug"],
                    },
                    {"repo": "acme/web", "nickname": "web", "description": "Web"},
                ],
            }
        },
    )
    assert parse_policy(render_policy(policy)) == policy


@pytest.mark.parametrize(
    "description",
    [
        'Has "quotes" and \\ backslashes',
        "Tabs\tand\nnewlines",
        "Control \x01 and delete \x7f",
        "Unicode: café, 日本語, emoji 🚀",
        "# not a comment",
        "ends with backslash \\",
    ],
)
def test_awkward_strings_survive(description: str) -> None:
    policy = _policy(
        channels={
            "C0ONE": {
                "repos": [{"repo": "acme/web", "nickname": "web", "description": description}]
            }
        }
    )
    assert parse_policy(render_policy(policy)) == policy


@pytest.mark.parametrize("value", ['a"b', "a\\b", "x\ny", "\x00", "🚀"])
def test_toml_string_is_valid_toml(value: str) -> None:
    assert tomllib.loads(f"k = {toml_string(value)}")["k"] == value


def test_output_has_a_header_and_no_secrets_hint() -> None:
    text = render_policy(_policy())
    assert text.startswith("# jotflow.toml")
    assert "xoxb" not in text
