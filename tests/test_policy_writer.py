from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from jotflow.policy import Policy, load_policy, parse_policy
from jotflow.policy_writer import render_policy, toml_string

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "jotflow.example.toml"


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
        approval={
            "approvers": ["U0AAA", "U0BBB"],
            "require_second_approver": True,
            "reactions": True,
        },
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


def test_an_unrestricted_repo_is_written_without_a_labels_line() -> None:
    text = render_policy(_policy())
    assert "labels" not in text.split("[[channels.C0ONE.repos]]", 1)[1]
    assert parse_policy(text) == _policy()


def test_a_label_list_is_written() -> None:
    policy = _policy(
        channels={
            "C0ONE": {
                "repos": [
                    {"repo": "acme/web", "nickname": "web", "description": "Web", "labels": ["bug"]}
                ]
            }
        }
    )
    text = render_policy(policy)
    assert 'labels = ["bug"]' in text
    assert parse_policy(text) == policy


def test_run_cap_round_trips() -> None:
    policy = _policy(agent={"max_concurrent_runs": 7})
    text = render_policy(policy)
    assert "max_concurrent_runs = 7" in text
    assert parse_policy(text) == policy


def test_help_settings_round_trip_and_only_the_overriding_channel_gets_a_table() -> None:
    repo = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}
    policy = Policy.model_validate(
        {
            "help": {"near_miss_nudge": False},
            "channels": {
                "C0ONE1": {"repos": [repo]},
                "C0TWO1": {"repos": [repo], "help": {"guide": False}},
            },
        }
    )
    text = render_policy(policy)
    assert parse_policy(text) == policy
    assert "near_miss_nudge = false" in text
    assert "[channels.C0TWO1.help]" in text and "[channels.C0ONE1.help]" not in text


def test_a_second_approver_survives_a_rewrite() -> None:
    # Setup rewrites jotflow.toml when channels change; the setting must stay (#244).
    policy = _policy(approval={"approvers": ["U0AAA"], "require_second_approver": True})
    text = render_policy(policy)
    assert "require_second_approver = true" in text
    assert parse_policy(text).approval.require_second_approver is True
    assert "require_second_approver = false" in render_policy(_policy())


def test_a_custom_approval_reaction_round_trips() -> None:
    policy = _policy(approval={"reactions": True, "reaction": "+1"})
    text = render_policy(policy)
    assert 'reaction = "+1"' in text


def test_repo_visibility_round_trips() -> None:
    policy = _policy(
        channels={
            "G0PRIV": {
                "repos": [
                    {
                        "repo": "acme/api",
                        "nickname": "api",
                        "description": "API",
                        "visibility": "public",
                    },
                    {"repo": "acme/web", "nickname": "web", "description": "Web"},
                ]
            }
        }
    )
    text = render_policy(policy)
    assert text.count("visibility = ") == 1 and 'visibility = "public"' in text
    assert parse_policy(text) == policy


@pytest.mark.parametrize("reaction", ["", "ticket", "tada"])
def test_the_issue_reaction_round_trips(reaction: str) -> None:
    policy = _policy(issue_reaction=reaction, approval={"reactions": True, "reaction": "+1"})
    text = render_policy(policy)
    assert f'issue_reaction = "{reaction}"' in text
    assert parse_policy(text) == policy


def test_conventions_and_title_pattern_survive_a_rewrite() -> None:
    # Setup rewrites jotflow.toml when channels change; the team's rules must stay (#216).
    repo = {
        "repo": "acme/web",
        "nickname": "web",
        "description": "Web",
        "title_pattern": r"^(feat|fix)(\([a-z-]+\))?: .+$",
        "conventions": 'Use "Severity: high" only for outages.\n- Always add a checklist.',
    }
    policy = _policy(channels={"C0WEB": {"repos": [repo]}})
    [back] = parse_policy(render_policy(policy)).channels["C0WEB"].repos
    assert back.title_pattern == repo["title_pattern"]
    assert back.conventions == repo["conventions"]


def test_scopes_survive_a_rewrite() -> None:
    scope = {
        "owner": "example-org",
        "match": ["web-*"],
        "exclude": ["*-demo"],
        "include_forks": True,
        "labels": ["bug"],
        "aliases": {"site": "web-frontend"},
        "description": "Front-end repos",
    }
    policy = _policy(channels={"C0WEB": {"scopes": [scope]}})
    assert parse_policy(render_policy(policy)) == policy


def test_question_settings_survive_a_rewrite() -> None:
    from jotflow.policy import Policy, parse_policy
    from jotflow.policy_writer import render_policy

    data = {
        "questions": {"enabled": True, "per_hour": 4, "per_person_seconds": 120},
        "channels": {
            "C0IDEAS01": {
                "repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}],
                "questions": {"enabled": False},
            }
        },
    }
    policy = Policy.model_validate(data)
    text = render_policy(policy)
    assert parse_policy(text) == policy
    assert "[questions]" in text and "[channels.C0IDEAS01.questions]" in text
    plain = Policy.model_validate(
        {
            "channels": data["channels"]
            | {"C0IDEAS01": {"repos": data["channels"]["C0IDEAS01"]["repos"]}}
        }
    )
    assert "[questions]" not in render_policy(plain)
