"""What JotFlow adds to every new issue: a label and a title prefix (#306)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from jotflow.commands.check import issue_notes
from jotflow.execution.writes import plan
from jotflow.models import IssueChanges, IssueProposal, ProposalRecord, UpdateProposal
from jotflow.orchestrator.cards import CardContext, render_card
from jotflow.policy import IssuesPolicy, PolicyError, parse_policy
from jotflow.policy_writer import render_policy

BASE = """
{issues}
[channels.C0EXAMPLE1]

[[channels.C0EXAMPLE1.repos]]
repo = "acme-co/web"
nickname = "web"
description = "Web"
{pattern}
"""


def policy(issues: str = "", pattern: str = "") -> Any:
    return parse_policy(BASE.format(issues=issues, pattern=pattern))


ON = '[issues]\nlabel = "from-slack"\ntitle_prefix = "[Slack]"\n'


def record(draft: IssueProposal | UpdateProposal | None = None) -> ProposalRecord:
    return ProposalRecord(
        id="p1",
        channel_id="C0EXAMPLE1",
        thread_ts="1792310400.000100",
        run_id="r1",
        proposal=draft
        or IssueProposal(
            repo="acme-co/web",
            title="Export the dashboard to CSV",
            body="Finance needs it.",
            labels=["enhancement"],
            confidence=0.9,
        ),
        created_at=datetime(2026, 10, 29, tzinfo=UTC),
    )


def test_both_are_off_by_default() -> None:
    issues = policy().issues
    assert issues == IssuesPolicy()
    assert issues.filed_title("Export") == "Export"
    assert issues.filed_labels(["bug"]) == ["bug"]
    checks, [write] = plan(record(), "tail", issues)
    assert write.arguments["title"] == "Export the dashboard to CSV"
    assert write.arguments["labels"] == ["enhancement"] == checks.labels


def test_a_new_issue_gets_the_prefix_and_label_and_the_label_is_checked() -> None:
    checks, [write] = plan(record(), "tail", policy(ON).issues)
    assert write.arguments["title"] == "[Slack] Export the dashboard to CSV"
    assert write.arguments["labels"] == ["enhancement", "from-slack"]
    # Checked before writing like the draft's own: a missing one stops the write.
    assert checks.labels == ["enhancement", "from-slack"]


def test_the_label_and_prefix_are_never_doubled() -> None:
    issues = policy(ON).issues
    assert issues.filed_labels(["From-Slack"]) == ["From-Slack"]
    assert issues.filed_title("[Slack] Already there") == "[Slack] Already there"
    # Only the prefix and a space count as already there.
    assert issues.filed_title("[Slack]ish idea") == "[Slack] [Slack]ish idea"


def test_the_filed_title_stays_within_githubs_limit() -> None:
    issues = policy(ON).issues
    long = "x" * 256
    assert len(issues.filed_title(long)) == 256
    assert issues.filed_title(long).startswith("[Slack] ")


def test_an_update_gets_neither() -> None:
    update = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(comment="More."),
        confidence=0.9,
    )
    _, writes = plan(record(update), "tail", policy(ON).issues)
    assert all("from-slack" not in str(w.arguments) for w in writes)
    assert all("[Slack]" not in str(w.arguments) for w in writes)


def test_the_card_shows_what_will_be_filed() -> None:
    settings = policy(ON)
    context = CardContext(channel=settings.channel("C0EXAMPLE1"), issues=settings.issues)
    text = str(render_card(record(), context))
    assert "[Slack] Export the dashboard to CSV" in text
    # JotFlow's label is marked as always added; the not-checked note is its own (#337).
    line = "Labels: enhancement, from-slack (added to every issue)"
    assert line in text


def test_the_card_marks_the_label_even_when_the_draft_has_none() -> None:
    settings = policy('[issues]\nlabel = "from-slack"\n')
    draft = record().proposal.model_copy(update={"labels": []})
    context = CardContext(channel=settings.channel("C0EXAMPLE1"), issues=settings.issues)
    assert "Labels: from-slack (added to every issue)" in str(render_card(record(draft), context))


@pytest.mark.parametrize(
    "bad",
    [
        'label = " padded"',
        'label = ""',
        'title_prefix = " [Slack]"',
        'title_prefix = "<!channel>"',
        'title_prefix = "@team"',
        'title_prefix = "' + "x" * 33 + '"',
        'title_prefix = ""',
        "unknown = 1",
    ],
)
def test_bad_settings_are_refused(bad: str) -> None:
    with pytest.raises(PolicyError):
        policy(f"[issues]\n{bad}\n")


def test_setup_keeps_the_settings_when_it_rewrites_the_file() -> None:
    settings = policy(ON)
    assert parse_policy(render_policy(settings)).issues == settings.issues
    assert "[issues]" not in render_policy(policy())  # a default file is unchanged


def test_rebuild_restores_titles_without_the_prefix() -> None:
    issues = policy(ON).issues
    assert issues.bare_title("[Slack] Export to CSV") == "Export to CSV"
    assert issues.bare_title("Export to CSV") == "Export to CSV"
    assert IssuesPolicy().bare_title("[Slack] Export") == "[Slack] Export"


def test_check_says_what_the_settings_need() -> None:
    assert issue_notes(policy()) == []
    [label] = issue_notes(policy('[issues]\nlabel = "from-slack"\n'))
    assert "'from-slack'" in label and "exists in each repo" in label
    [_, prefix] = issue_notes(policy(ON))
    assert "'[Slack] '" in prefix and "title_pattern" not in prefix
    notes = issue_notes(policy(ON, pattern='title_pattern = "^[A-Z].*"'))
    assert len(notes) == 2 and "acme-co/web" in notes[1] and "won't match" in notes[1]
