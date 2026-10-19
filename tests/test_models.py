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
        id="p1",
        channel_id="C0TEST1",
        thread_ts="1700000000.000100",
        run_id="r1",
        proposal=update,
        created_at=NOW,
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
            thread_ts="1700000000.000100",
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
        "https://github.com/acmé/web/issues/1",
        "https://github.com/acme/web/issues/\u0661",
    ],
)
def test_result_rejects_non_issue_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(issue_url=url)


CREATE = IssueProposal(repo="acme/web", title="x", body="", confidence=0.5)
UPDATE = UpdateProposal(
    topic_id="t1",
    repo="acme/web",
    issue_number=7,
    changes=IssueChanges(comment="more"),
    confidence=0.5,
)


def test_create_result_must_be_an_issue_in_the_approved_repo() -> None:
    result = ExecutionResult(issue_url="https://github.com/Acme/Web/issues/42")
    assert result.issue_ref() == ("Acme/Web", 42)
    assert result.matches(CREATE)
    assert not result.matches(CREATE.model_copy(update={"repo": "acme/api"}))


def test_create_result_rejects_a_comment_url() -> None:
    result = ExecutionResult(issue_url="https://github.com/acme/web/issues/7#issuecomment-123")
    assert not result.matches(CREATE)


def test_update_result_must_be_the_proposals_issue() -> None:
    assert ExecutionResult(issue_url="https://github.com/acme/web/issues/7").matches(UPDATE)
    comment = ExecutionResult(issue_url="https://github.com/acme/web/issues/7#issuecomment-1")
    assert comment.matches(UPDATE)
    assert not ExecutionResult(issue_url="https://github.com/acme/web/issues/8").matches(UPDATE)
    assert not ExecutionResult(issue_url="https://github.com/acme/api/issues/7").matches(UPDATE)


def test_error_result_matches_nothing() -> None:
    assert not ExecutionResult(error="rate limited").matches(CREATE)


@pytest.mark.parametrize("repo", ["acme/.", "acme/..", "-acme/web", "a" * 40 + "/web", "acme/"])
def test_invalid_repo_names_are_rejected(repo: str) -> None:
    with pytest.raises(ValidationError):
        IssueProposal(repo=repo, title="x", body="", confidence=0.5)


def test_dot_repo_urls_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(issue_url="https://github.com/acme/../issues/1")


def test_repo_names_are_ascii_only() -> None:
    with pytest.raises(ValidationError):
        IssueProposal(repo="acmé/web", title="x", body="", confidence=0.5)


# The overruled hint (#23, D10)


def test_an_unset_hint_overruled_keeps_hashes_from_before_it_existed() -> None:
    from jotflow.models import IssueChanges, IssueProposal, UpdateProposal
    from jotflow.store.hashing import content_hash

    # Hashes computed on main before the field was added.
    issue = IssueProposal(
        repo="acme-co/web",
        title="Progress bar",
        body="Show progress.",
        labels=["enhancement"],
        confidence=0.9,
    )
    update = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(comment="More detail."),
        confidence=0.8,
    )
    assert content_hash(issue) == (
        "sha256:23ba7c39abb81950ceab0080d9dbc154cdedba4c6fe2c0de1711b21ec1bf154c"
    )
    assert content_hash(update) == (
        "sha256:ab8dc602273e0b8be672a788c96e6426904a0b1b14d76b2a55b0fd90ba53c82a"
    )
    overruled = issue.model_copy(update={"hint_overruled": "It is a bug, not an idea"})
    assert content_hash(overruled) != content_hash(issue)  # approval locks it too


def test_hint_overruled_is_one_short_line_or_nothing() -> None:
    import pytest
    from pydantic import ValidationError

    from jotflow.models import IssueProposal

    base = {"repo": "acme-co/web", "title": "t", "body": "b", "confidence": 0.5}
    assert IssueProposal.model_validate(base).hint_overruled is None
    for bad in ("", "x" * 201, "two\nlines"):
        with pytest.raises(ValidationError):
            IssueProposal.model_validate({**base, "hint_overruled": bad})


@pytest.mark.parametrize(
    "bad", ["it‮is a bug", "zero​width", " leading space", "trailing ", "tab\there"]
)
def test_hint_overruled_follows_the_label_character_rule(bad: str) -> None:
    from pydantic import ValidationError

    from jotflow.models import IssueChanges, IssueProposal, UpdateProposal

    with pytest.raises(ValidationError, match="hint_overruled"):
        IssueProposal(repo="acme-co/web", title="t", body="b", confidence=0.5, hint_overruled=bad)
    with pytest.raises(ValidationError, match="hint_overruled"):
        UpdateProposal(
            topic_id="t1",
            repo="acme-co/web",
            issue_number=1,
            changes=IssueChanges(),
            confidence=0.5,
            hint_overruled=bad,
        )


def test_the_agents_repo_visibility_is_outside_the_approval_hash() -> None:
    from jotflow.models import IssueProposal
    from jotflow.store.hashing import content_hash

    plain = IssueProposal(repo="a/b", title="T", body="B", confidence=0.5)
    reported = plain.model_copy(update={"repo_visibility": "public"})
    assert content_hash(plain) == content_hash(reported)
    with pytest.raises(ValueError):
        IssueProposal(repo="a/b", title="T", body="B", confidence=0.5, repo_visibility="internal")
