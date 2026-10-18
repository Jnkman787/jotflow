from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from jotflow.models import (
    ExecutionResult,
    IssueChanges,
    IssueProposal,
    ProposalRecord,
    ProposalState,
    Run,
    RunKind,
    RunStatus,
    UpdateProposal,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_issue_proposal_round_trips() -> None:
    proposal = IssueProposal(
        topic_id="t1",
        repo="acme/web",
        title="Add a progress bar to onboarding",
        body="Raised in the ideas channel.",
        labels=["enhancement"],
        confidence=0.9,
    )
    assert IssueProposal.model_validate_json(proposal.model_dump_json()) == proposal


def test_repo_must_be_owner_slash_name() -> None:
    with pytest.raises(ValidationError):
        IssueProposal(topic_id="t1", repo="not-a-repo", title="x", body="", confidence=0.5)


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        UpdateProposal(
            topic_id="t1",
            repo="o/r",
            issue_number=1,
            changes=IssueChanges(comment="hi"),
            confidence=1.5,
        )


def test_cli_reports_version() -> None:
    from jotflow import __version__
    from jotflow.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__


def test_new_topic_proposal_needs_no_topic_id() -> None:
    proposal = IssueProposal(repo="acme/web", title="x", body="", confidence=0.5)
    assert proposal.topic_id is None


def test_proposal_states_follow_the_lifecycle() -> None:
    assert {s.value for s in ProposalState} == {
        "pending",
        "approved",
        "executing",
        "reconciling",
        "applied",
        "rejected",
        "superseded",
        "failed",
    }


def test_proposal_record_round_trips_either_kind() -> None:
    update = UpdateProposal(
        topic_id="t1",
        repo="acme/web",
        issue_number=7,
        changes=IssueChanges(comment="more detail"),
        confidence=0.8,
    )
    record = ProposalRecord(
        id="p1", channel_id="C0TEST1", run_id="r1", proposal=update, created_at=NOW
    )
    assert record.state is ProposalState.PENDING
    restored = ProposalRecord.model_validate_json(record.model_dump_json())
    assert isinstance(restored.proposal, UpdateProposal)
    assert restored == record


def test_execution_run_needs_a_proposal() -> None:
    with pytest.raises(ValidationError, match="proposal_id"):
        Run(
            id="r1",
            kind=RunKind.EXECUTION,
            channel_id="C0TEST1",
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )


def test_drafting_run_defaults_to_queued() -> None:
    run = Run(
        id="r1",
        kind=RunKind.DRAFTING,
        channel_id="C0TEST1",
        thread_ts="1700000000.000100",
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )
    assert run.status is RunStatus.QUEUED


def test_result_needs_exactly_one_outcome() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ExecutionResult()
    with pytest.raises(ValidationError, match="exactly one"):
        ExecutionResult(issue_url="https://github.com/acme/web/issues/1", error="boom")
    assert ExecutionResult(error="rate limited").issue_ref() is None


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/web/issues/1",
        "https://github.com/acme/web/pull/1",
        "https://github.com/acme/web/issues/0",
        "https://example.com/acme/web/issues/1",
        "https://github.com/acme/web/issues/1/../../other/issues/2",
    ],
)
def test_result_rejects_non_issue_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(issue_url=url)


def test_result_url_must_point_at_the_approved_repo() -> None:
    result = ExecutionResult(issue_url="https://github.com/Acme/Web/issues/42")
    assert result.issue_ref() == ("Acme/Web", 42)
    assert result.points_at("acme/web")
    assert result.points_at("acme/web", 42)
    assert not result.points_at("acme/web", 43)
    assert not result.points_at("acme/api")


def test_result_accepts_a_comment_url_for_updates() -> None:
    result = ExecutionResult(issue_url="https://github.com/acme/web/issues/7#issuecomment-123")
    assert result.points_at("acme/web", 7)
