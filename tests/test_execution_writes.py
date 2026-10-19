"""The exact writes JotFlow plans for an approved proposal (#31)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jotflow.execution.writes import clean_name, footer, marker, plan
from jotflow.models import (
    IssueChanges,
    IssueProposal,
    ProposalRecord,
    ProposalState,
    UpdateProposal,
)

LINK = "https://acme.slack.com/archives/C0EXAMPLE1/p1792310400000100"


def record(proposal: IssueProposal | UpdateProposal) -> ProposalRecord:
    return ProposalRecord(
        id="a1b2c3d4e5f60718",
        channel_id="C0EXAMPLE1",
        thread_ts="1792310400.000100",
        run_id="run-1",
        state=ProposalState.APPROVED,
        proposal=proposal,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )


def test_names_cannot_mention_link_or_format() -> None:
    assert clean_name("@sam [click](https://x.example) `code` <@U1>\nline") == (
        "sam click https x.example code U1 line"
    )
    assert clean_name("Sam www.evil.example") == "Sam www evil.example"
    # HTML entities GitHub would render as a mention, an issue link, or a URL.
    for entity in ("&#64;torvalds", "&#x40;torvalds", "&commat;torvalds"):
        assert "&" not in clean_name(entity) and ";" not in clean_name(entity)
    assert clean_name("&#64;torvalds") == "64 torvalds"
    assert clean_name("&commat;torvalds") == "commat torvalds"
    assert clean_name("issue &#35;12") == "issue 35 12"
    assert clean_name("https&#58;&#47;&#47;evil.example") == "https 58 47 47 evil.example"
    # Bidi overrides and zero-width characters cannot reorder or hide a name.
    assert clean_name("Sam\u202eevil") == "Samevil"
    assert clean_name("S\u200bam") == "Sam"
    assert clean_name("Sam\x07Lee") == "Sam Lee"
    assert clean_name("A" * 60) == "A" * 40
    assert clean_name("  Dana   Lee ") == "Dana Lee"


def test_the_footer_names_people_and_links_then_marks() -> None:
    tail = footer(
        "p1",
        poster="Sam",
        others=["Sam", "Alex", "Bob"],
        permalinks=[LINK, "https://evil.example/x"],
    )
    assert tail.splitlines() == [
        "Raised in Slack by Sam, with Alex and Bob looped in.",
        f"Slack: {LINK}",
        "<!-- jotflow:proposal:p1 -->",
    ]
    assert footer("p1", poster=None, others=["Alex"]).splitlines()[0] == (
        "Raised in Slack, with Alex looped in."
    )
    assert footer("p1", poster="Sam").splitlines()[0] == "Raised in Slack by Sam."
    assert footer("p1", poster=None).splitlines()[0] == "Raised in Slack."
    many = footer("p1", poster="P", others=[f"N{i}" for i in range(15)])
    assert "N9 looped in" in many and "N10" not in many


def test_issue_links_none_leaves_only_the_marker() -> None:
    assert footer("p1", poster="Sam", others=["Alex"], permalinks=[LINK], issue_links="none") == (
        marker("p1")
    )


def test_a_create_is_one_exact_issue_write() -> None:
    proposal = IssueProposal(
        repo="acme-co/web",
        title="Export the dashboard table as CSV",
        body="Finance needs a monthly export.\n",
        labels=["enhancement"],
        assignees=["acme-bob"],
        confidence=0.9,
    )
    tail = footer("a1b2c3d4e5f60718", poster="Sam")
    checks, writes = plan(record(proposal), tail)
    assert checks.marker == "<!-- jotflow:proposal:a1b2c3d4e5f60718 -->"
    assert (checks.marker_in, checks.labels, checks.issue_number) == (
        "recent_issues",
        ["enhancement"],
        None,
    )
    [write] = writes
    assert write.tool == "issue_write"
    assert write.arguments == {
        "method": "create",
        "owner": "acme-co",
        "repo": "web",
        "title": "Export the dashboard table as CSV",
        "body": f"Finance needs a monthly export.\n\n---\n{tail}",
        "labels": ["enhancement"],
        "assignees": ["acme-bob"],
    }
    assert write.labels_from_current is None


def test_a_create_without_labels_or_assignees_sends_neither() -> None:
    proposal = IssueProposal(repo="acme-co/web", title="t", body="", confidence=0.5)
    [write] = plan(record(proposal), marker("x"))[1]
    assert "labels" not in write.arguments and "assignees" not in write.arguments
    assert write.arguments["body"] == marker("x")


def test_a_comment_update_is_one_comment_carrying_the_marker() -> None:
    proposal = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(comment="Also keep the column order."),
        confidence=0.8,
    )
    checks, writes = plan(record(proposal), "TAIL")
    assert (checks.marker_in, checks.issue_number, checks.labels) == ("issue_comments", 42, [])
    [write] = writes
    assert write.tool == "add_issue_comment"
    assert write.arguments == {
        "owner": "acme-co",
        "repo": "web",
        "issue_number": 42,
        "body": "Also keep the column order.\n\n---\nTAIL",
    }


def test_an_update_with_everything_edits_then_comments() -> None:
    proposal = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(
            body="New body.",
            comment="Why it changed.",
            add_labels=["bug"],
            remove_labels=["enhancement"],
            add_assignees=["acme-bob"],
        ),
        confidence=0.8,
    )
    checks, writes = plan(record(proposal), "TAIL")
    assert checks.marker_in == "issue_comments" and checks.labels == ["bug"]
    edit, comment = writes
    assert edit.arguments == {
        "method": "update",
        "owner": "acme-co",
        "repo": "web",
        "issue_number": 42,
        "body": "New body.\n\n---\nTAIL",  # the footer stays on a replaced body (#167)
    }
    assert edit.markers_from_current is True
    assert edit.labels_from_current is not None
    assert (edit.labels_from_current.add, edit.labels_from_current.remove) == (
        ["bug"],
        ["enhancement"],
    )
    assert edit.assignees_from_current is not None
    assert edit.assignees_from_current.add == ["acme-bob"]
    assert comment.tool == "add_issue_comment"


def test_a_body_only_update_marks_the_body() -> None:
    proposal = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=7,
        changes=IssueChanges(body="Rewritten."),
        confidence=0.8,
    )
    checks, [edit] = plan(record(proposal), "TAIL")
    assert checks.marker_in == "issue_body"
    assert edit.arguments["body"] == "Rewritten.\n\n---\nTAIL"
    assert edit.markers_from_current is True
    assert edit.labels_from_current is None


@pytest.mark.parametrize("remove_only", [True, False])
def test_a_labels_only_update_has_nothing_to_mark(remove_only: bool) -> None:
    changes = (
        IssueChanges(remove_labels=["wontfix"]) if remove_only else IssueChanges(add_labels=["bug"])
    )
    proposal = UpdateProposal(
        topic_id="t1", repo="acme-co/web", issue_number=7, changes=changes, confidence=0.8
    )
    checks, [edit] = plan(record(proposal), "TAIL")
    assert checks.marker_in == "nowhere"
    assert "body" not in edit.arguments
    assert edit.markers_from_current is False
    assert edit.labels_from_current is not None


def test_a_replaced_body_keeps_every_marker_the_old_one_had() -> None:
    from jotflow.execution.writes import with_kept_markers

    created = marker("0123456789abcdef")
    earlier = marker("fedcba9876543210")
    planned = f"New body.\n\n---\nRaised in Slack.\n{marker('a1b2c3d4e5f60718')}"
    old = f"Old body.\n\n---\nRaised in Slack by Sam.\n{created}\n{earlier}\n{created}"
    assert with_kept_markers(planned, old) == f"{planned}\n{created}\n{earlier}"


def test_an_issue_jotflow_did_not_create_gains_only_the_update_footer() -> None:
    from jotflow.execution.writes import with_kept_markers

    planned = f"New body.\n\n---\n{marker('a1b2c3d4e5f60718')}"
    # Marker-like text inline, or a different comment, is not a marker line.
    old = "Someone wrote this by hand. <!-- jotflow:proposal:x --> inline\n<!-- note -->"
    assert with_kept_markers(planned, old) == planned
    assert with_kept_markers(planned, planned) == planned
