import pytest
from pydantic import ValidationError

from jotflow.models import IssueChanges, IssueProposal, UpdateProposal


def test_issue_proposal_round_trips() -> None:
    proposal = IssueProposal(
        topic_id="t1",
        repo="Jnkman787/jotflow",
        title="Add a progress bar to onboarding",
        body="Raised in #product.",
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
