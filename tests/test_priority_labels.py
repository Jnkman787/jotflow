"""A stated priority, such as P0, becomes a configured label (#332)."""

from __future__ import annotations

from typing import Any

import pytest

from jotflow.models import IssueProposal, ProposalState, RunKind
from jotflow.orchestrator.thread_notice import skipped_line
from jotflow.policy import Policy, PolicyError, parse_policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore
from jotflow.store.base import AUTO_APPROVER
from jotflow.whiteboard.read_tools import read_tools
from tests.test_issues_snapshot import PID, Labels, record
from tests.test_read_tools import FakeSource, call, checked_registry, running

CH = "C0IDEAS01"
CRITICAL = "priority: critical"


def toml(channel: str = "", repo: str = "", labels: str = "") -> str:
    return (
        f'[channels.{CH}]\n{channel}\n\n[[channels.{CH}.repos]]\nrepo = "acme-co/web"\n'
        f'nickname = "web"\ndescription = "Web"\n{labels}\n{repo}\n'
    )


def test_a_channel_and_a_repo_can_map_priorities_and_round_trip() -> None:
    text = toml(
        channel=f'priority_labels = {{ "P0" = "{CRITICAL}", "urgent" = "{CRITICAL}" }}',
        repo='priority_labels = { "sev1" = "sev-1" }',
    )
    policy = parse_policy(text)
    channel = policy.channels[CH]
    assert channel.priority_labels == {"P0": CRITICAL, "urgent": CRITICAL}
    assert channel.priority_labels_for(channel.repos[0]) == {"sev1": "sev-1"}  # replaces it
    assert parse_policy(render_policy(policy)) == policy


@pytest.mark.parametrize(
    ("channel", "repo", "labels", "error"),
    [
        (f'priority_labels = {{ "P0" = "{CRITICAL}", "p0" = "x" }}', "", "", "more than once"),
        ("", 'priority_labels = { "P1" = "a", "p1" = "b" }', "", "more than once"),
        (f'priority_labels = {{ "P0" = "{CRITICAL}" }}', "", 'labels = ["bug"]', "isn't in web"),
        ("", 'priority_labels = { "P0" = "sev" }', 'labels = ["bug"]', "isn't in web"),
        ('priority_labels = { " P0" = "x" }', "", "", "no outer spaces"),
        ('priority_labels = { "P0" = "bad\\nlabel" }', "", "", "label"),
    ],
)
def test_a_bad_map_fails_at_load(channel: str, repo: str, labels: str, error: str) -> None:
    with pytest.raises(PolicyError, match=error):
        parse_policy(toml(channel=channel, repo=repo, labels=labels))


def test_a_channel_map_must_fit_each_restricted_repo_and_scope() -> None:
    fits = toml(
        channel=f'priority_labels = {{ "P0" = "{CRITICAL}" }}',
        labels=f'labels = ["bug", "{CRITICAL}"]',
    )
    assert parse_policy(fits)
    scoped = (
        f'[channels.{CH}]\npriority_labels = {{ "P0" = "{CRITICAL}" }}\n\n'
        f'[[channels.{CH}.scopes]]\nowner = "acme-co"\nlabels = ["bug"]\n'
    )
    with pytest.raises(PolicyError, match="isn't in acme-co's labels"):
        parse_policy(scoped)


def policy_with_map() -> Policy:
    return parse_policy(toml(channel=f'priority_labels = {{ "P0" = "{CRITICAL}" }}'))


async def work_for(store: MemoryStore, labels: Labels | None, draft_labels: list[str]) -> Any:
    draft = IssueProposal(
        repo="acme-co/web", title="Checkout", body="Fails.", labels=draft_labels, confidence=0.9
    )
    await store.add_proposal(record(proposal=draft, channel_id=CH, thread_ts="1760781600.000100"))
    await store.transition(PID, ProposalState.APPROVED, approved_by=AUTO_APPROVER)
    grant = await running(store, RunKind.EXECUTION, proposal_id=PID, thread="1760781600.000100")
    specs = checked_registry(
        read_tools(store, policy_with_map(), FakeSource(), labels=lambda: labels)
    )
    return (await call(specs["get_work"], grant)).execution


async def test_a_missing_priority_label_is_skipped_and_named() -> None:
    store = MemoryStore()
    labels = Labels(False)
    work = await work_for(store, labels, ["bug", CRITICAL])
    [write] = work.writes
    assert write.arguments["labels"] == ["bug"] and work.checks.labels == ["bug"]
    assert labels.asked == [("acme-co", "web", CRITICAL)]  # only the priority label
    found = await store.get_proposal(PID)
    assert found is not None and found.skipped_label == CRITICAL


async def test_a_priority_label_that_exists_is_kept() -> None:
    work = await work_for(MemoryStore(), Labels(True), [CRITICAL])
    assert work.checks.labels == [CRITICAL]


async def test_an_unchecked_priority_label_stays_so_the_write_stops() -> None:
    work = await work_for(MemoryStore(), None, [CRITICAL])
    assert work.checks.labels == [CRITICAL]


def test_the_thread_names_every_skipped_label() -> None:
    both = record(skipped_label="from slack\npriority: <critical>")
    assert skipped_line(both) == (
        "Filed without the labels from slack and priority: &lt;critical&gt;, which don't "
        "exist in acme-co/web."
    )
    assert skipped_line(record(skipped_label="x")) == (
        "Filed without the label x, which doesn't exist in acme-co/web."
    )


async def execute_with(store: MemoryStore, approved_under: Policy, run_under: Policy) -> Any:
    """Approve under one policy and write under another, as after a restart (#359 review)."""
    from jotflow.orchestrator.approval import approved_issues
    from jotflow.repo_catalog import RepoCatalog

    draft = IssueProposal(
        repo="acme-co/web", title="Checkout", body="Fails.", labels=[CRITICAL], confidence=0.9
    )
    added = await store.add_proposal(
        record(proposal=draft, channel_id=CH, thread_ts="1760781600.000100")
    )
    locked = approved_issues(approved_under, RepoCatalog.static(approved_under), added)
    await store.transition(PID, ProposalState.APPROVED, approved_by=AUTO_APPROVER, issues=locked)
    grant = await running(store, RunKind.EXECUTION, proposal_id=PID, thread="1760781600.000100")
    labels = Labels(False)
    specs = checked_registry(read_tools(store, run_under, FakeSource(), labels=lambda: labels))
    return (await call(specs["get_work"], grant)).execution, labels


async def test_the_priority_map_is_the_one_approved_not_todays() -> None:
    no_map = parse_policy(toml())
    # Approved as a stated priority, then the map was removed: still skipped if missing.
    work, labels = await execute_with(MemoryStore(), policy_with_map(), no_map)
    assert work.checks.labels == [] and labels.asked == [("acme-co", "web", CRITICAL)]
    # Approved as an ordinary label, then mapped: still stops the write if missing.
    work, labels = await execute_with(MemoryStore(), no_map, policy_with_map())
    assert work.checks.labels == [CRITICAL] and labels.asked == []
