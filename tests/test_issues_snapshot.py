"""[issues] settings locked at approval, and a missing [issues] label skipped (#306 follow-up)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters.github_repos import GithubError, RateLimited, label_exists
from jotflow.models import (
    ChannelMode,
    ExecutionResult,
    IssueProposal,
    ProposalRecord,
    ProposalState,
    RunKind,
)
from jotflow.orchestrator.approval import ISSUES_CHANGED, ApprovalHandler
from jotflow.orchestrator.launch import GithubBusy, RunnerError
from jotflow.orchestrator.modes import AutoApprover
from jotflow.orchestrator.thread_notice import skipped_line
from jotflow.policy import IssuesPolicy, Policy
from jotflow.store import MemoryStore, SqliteStore, Store, content_hash
from jotflow.store.base import AUTO_APPROVER
from jotflow.whiteboard.read_tools import read_tools
from tests.test_approval import ALICE, BOB, CARD_TS, CH, Slack, click
from tests.test_github_people import client as runner_client
from tests.test_github_people import status
from tests.test_github_repos import FakeGithub, runner_http
from tests.test_read_tools import FakeSource, call, checked_registry, running
from tests.test_runner import READ

PID = "0123456789abcdef"
LABEL = "from slack/agent"  # a space and a slash, which a URL path would mangle
SLACK = IssuesPolicy(label=LABEL, title_prefix="[Slack]")
OLD = IssuesPolicy(label="old-label")


def policy(issues: IssuesPolicy | None = None) -> Policy:
    data: dict[str, Any] = {
        "channels": {
            CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]}
        }
    }
    if issues is not None:
        data["issues"] = issues.model_dump(exclude_none=True)
    return Policy.model_validate(data)


def record(**update: Any) -> ProposalRecord:
    draft = IssueProposal(repo="acme-co/web", title="Export", body="CSV.", confidence=0.9)
    base = ProposalRecord(
        id=PID,
        channel_id=CH,
        thread_ts="1760781600.000100",
        run_id="r0",
        proposal=draft,
        created_at=datetime(2026, 10, 18, 8, 0, tzinfo=UTC),
    )
    return base.model_copy(update=update)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


# --- the snapshot -------------------------------------------------------------------------


def test_a_snapshot_round_trips_and_an_old_row_falls_back() -> None:
    assert IssuesPolicy.from_snapshot(SLACK.snapshot(), OLD) == SLACK
    assert IssuesPolicy.from_snapshot(None, OLD) == OLD
    assert SLACK.snapshot() == IssuesPolicy(title_prefix="[Slack]", label=LABEL).snapshot()


async def test_the_store_keeps_what_the_card_showed_and_what_was_approved(store: Store) -> None:
    added = await store.add_proposal(record())
    await store.mark_card_shown(PID, added.revision, SLACK.snapshot())
    await store.mark_card_shown(PID, added.revision)  # no settings given keeps them
    shown = await store.get_proposal(PID)
    assert shown is not None and shown.card_issues == SLACK.snapshot()
    with pytest.raises(ValueError, match="only apply when approving"):
        await store.transition(PID, ProposalState.REJECTED, issues=SLACK.snapshot())
    approved = await store.transition(
        PID,
        ProposalState.APPROVED,
        approved_by=ALICE,
        expected_hash=content_hash(added.proposal),
        issues=SLACK.snapshot(),
    )
    assert approved.approved_issues == SLACK.snapshot()
    # An interrupted write's retry keeps the original approval and its settings.
    await store.transition(PID, ProposalState.EXECUTING)
    await store.transition(PID, ProposalState.RECONCILING)
    again = await store.transition(PID, ProposalState.APPROVED, approved_by=ALICE)
    assert again.approved_issues == SLACK.snapshot()
    await store.set_skipped_label(PID, LABEL)
    found = await store.get_proposal(PID)
    assert found is not None and found.skipped_label == LABEL


async def test_a_retry_of_a_failed_proposal_locks_the_settings_again(store: Store) -> None:
    added = await store.add_proposal(record())
    locked = content_hash(added.proposal)
    await store.transition(
        PID, ProposalState.APPROVED, approved_by=ALICE, expected_hash=locked, issues="{}"
    )
    await store.transition(PID, ProposalState.EXECUTING)
    await store.transition(PID, ProposalState.FAILED, result=ExecutionResult(error="no"))
    retried = await store.transition(
        PID,
        ProposalState.APPROVED,
        approved_by=BOB,
        expected_hash=locked,
        issues=SLACK.snapshot(),
    )
    assert retried.approved_issues == SLACK.snapshot()


# --- approval ---------------------------------------------------------------------------


class Gate:
    def __init__(self, issues: IssuesPolicy | None) -> None:
        self.store = MemoryStore()
        self.redrawn: list[str] = []
        self.slack = Slack()
        self.handler = ApprovalHandler(self.store, policy(issues), self.slack, redraw=self._redraw)

    async def _redraw(self, proposal_id: str) -> None:
        self.redrawn.append(proposal_id)

    async def card(self, shown: IssuesPolicy | None) -> str:
        added = await self.store.add_proposal(record())
        await self.store.set_card_ts(PID, CARD_TS)
        snapshot = None if shown is None else shown.snapshot()
        await self.store.mark_card_shown(PID, added.revision, snapshot)
        return f"{PID}:{content_hash(added.proposal)}"

    async def state(self) -> ProposalRecord:
        found = await self.store.get_proposal(PID)
        assert found is not None
        return found


async def test_a_card_showing_the_current_settings_approves_and_locks_them() -> None:
    gate = Gate(SLACK)
    value = await gate.card(SLACK)
    assert await gate.handler.on_approve(click(value)) == "approved"
    assert (await gate.state()).approved_issues == SLACK.snapshot()
    assert gate.redrawn == []


@pytest.mark.parametrize("shown", [OLD, None])
async def test_a_card_drawn_with_other_settings_is_redrawn_not_approved(
    shown: IssuesPolicy | None,
) -> None:
    gate = Gate(SLACK)
    value = await gate.card(shown)
    assert await gate.handler.on_approve(click(value)) == "refused"
    assert gate.slack.ephemerals[-1] == (CH, BOB, ISSUES_CHANGED)
    assert gate.redrawn == [PID]
    assert (await gate.state()).state is ProposalState.PENDING


async def test_an_old_card_approves_when_nothing_is_added_then_or_now() -> None:
    gate = Gate(None)
    value = await gate.card(None)
    assert await gate.handler.on_approve(click(value)) == "approved"
    assert (await gate.state()).approved_issues == IssuesPolicy().snapshot()


async def test_auto_mode_locks_the_settings_too() -> None:
    store = MemoryStore()
    await store.add_proposal(record())
    await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by=ALICE)

    async def mode_for(_: str) -> ChannelMode:
        return ChannelMode.AUTO

    assert await AutoApprover(store, mode_for, SLACK).approve_due() == 1
    found = await store.get_proposal(PID)
    assert found is not None and found.approved_issues == SLACK.snapshot()
    assert found.approved_by == AUTO_APPROVER


# --- execution ----------------------------------------------------------------------------


class Labels:
    def __init__(self, answer: bool | Exception) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, str]] = []

    async def label_exists(self, owner: str, name: str, label: str) -> bool:
        self.asked.append((owner, name, label))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def execution(
    store: MemoryStore,
    labels: Labels | None,
    *,
    approved: IssuesPolicy | None,
    now: IssuesPolicy = OLD,
    draft_labels: list[str] | None = None,
) -> Any:
    draft = IssueProposal(
        repo="acme-co/web",
        title="Export",
        body="CSV.",
        labels=draft_labels or [],
        confidence=0.9,
    )
    await store.add_proposal(record(proposal=draft, thread_ts="1760781600.000100"))
    await store.transition(
        PID,
        ProposalState.APPROVED,
        approved_by=AUTO_APPROVER,
        issues=None if approved is None else approved.snapshot(),
    )
    grant = await running(store, RunKind.EXECUTION, proposal_id=PID, thread="1760781600.000100")
    specs = checked_registry(read_tools(store, policy(now), FakeSource(), labels=lambda: labels))
    work = await call(specs["get_work"], grant)
    return work.execution


async def test_execution_writes_what_was_approved_not_todays_settings() -> None:
    store = MemoryStore()
    labels = Labels(True)
    work = await execution(store, labels, approved=SLACK, now=OLD)
    [write] = work.writes
    assert write.arguments["title"] == "[Slack] Export"
    assert write.arguments["labels"] == [LABEL]
    assert work.checks.labels == [LABEL]
    assert labels.asked == [("acme-co", "web", LABEL)]


async def test_a_label_github_says_is_missing_is_left_off_and_recorded() -> None:
    store = MemoryStore()
    work = await execution(store, Labels(False), approved=SLACK)
    [write] = work.writes
    assert "labels" not in write.arguments and work.checks.labels == []
    found = await store.get_proposal(PID)
    assert found is not None and found.skipped_label == LABEL


@pytest.mark.parametrize("failure", [GithubBusy("busy"), RunnerError("down"), RuntimeError("x")])
async def test_a_label_that_couldnt_be_checked_stays_so_the_write_stops(
    failure: Exception,
) -> None:
    store = MemoryStore()
    work = await execution(store, Labels(failure), approved=SLACK)
    assert work.checks.labels == [LABEL]
    found = await store.get_proposal(PID)
    assert found is not None and found.skipped_label is None


async def test_without_the_drafting_runner_the_label_stays() -> None:
    work = await execution(MemoryStore(), None, approved=SLACK)
    assert work.checks.labels == [LABEL]


async def test_a_label_the_draft_has_itself_is_never_asked_about() -> None:
    labels = Labels(False)
    work = await execution(MemoryStore(), labels, approved=SLACK, draft_labels=[LABEL])
    assert labels.asked == [] and work.checks.labels == [LABEL]


async def test_a_later_call_keeps_the_earlier_skip_without_asking_again() -> None:
    store = MemoryStore()
    labels = Labels(False)
    await execution(store, labels, approved=SLACK)
    found = await store.get_proposal(PID)
    assert found is not None
    # The run asks for its work again, as after a reconnect: the same plan, no new ask.
    grant_runs = await store.thread_runs(CH, "1760781600.000100")
    from jotflow.whiteboard.tokens import Grant

    run = grant_runs[-1]
    grant = Grant(
        run_id=run.id,
        kind=RunKind.EXECUTION,
        channel_id=CH,
        thread_ts=run.thread_ts,
        proposal_id=PID,
    )
    specs = checked_registry(read_tools(store, policy(OLD), FakeSource(), labels=lambda: labels))
    work = (await call(specs["get_work"], grant)).execution
    assert work.checks.labels == [] and len(labels.asked) == 1


async def test_a_proposal_approved_before_snapshots_uses_todays_settings() -> None:
    work = await execution(MemoryStore(), Labels(True), approved=None, now=SLACK)
    [write] = work.writes
    assert write.arguments["title"] == "[Slack] Export"


def test_the_thread_hears_about_a_skipped_label() -> None:
    assert skipped_line(record()) is None
    line = skipped_line(record(skipped_label="a<b>&c"))
    assert line == "Filed without the label a&lt;b&gt;&amp;c, which doesn't exist in acme-co/web."


# --- the GitHub call, the runner and the client -------------------------------------------


async def test_the_label_is_encoded_into_githubs_path() -> None:
    encoded = "/repos/acme-co/web/labels/from%20slack%2Fagent"
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        code = 200 if request.url.raw_path.decode() == encoded else 404
        return httpx2.Response(code, json={})

    from jotflow.adapters import github_repos

    http = github_repos.client(transport=httpx2.MockTransport(handle))
    assert await label_exists("acme-co", "web", LABEL, READ, http=http)
    assert seen[0].url.raw_path.decode() == encoded
    assert not await label_exists("acme-co", "web", "nope", READ, http=http)
    with pytest.raises(GithubError):
        await label_exists("acme-co", "..", LABEL, READ, http=http)
    with pytest.raises(GithubError):
        await label_exists("acme-co", "web", " padded ", READ, http=http)
    assert len(seen) == 2


async def test_a_rate_limit_or_odd_answer_is_never_a_no() -> None:
    limited = FakeGithub({"/repos/acme-co/web/labels/bug": status(429)})
    with pytest.raises(RateLimited):
        await label_exists("acme-co", "web", "bug", READ, http=limited.client())
    odd = FakeGithub({"/repos/acme-co/web/labels/bug": status(500)})
    with pytest.raises(GithubError):
        await label_exists("acme-co", "web", "bug", READ, http=odd.client())


async def test_the_runner_takes_a_strict_body_only(tmp_path: Path) -> None:
    fake = FakeGithub({"/repos/acme-co/web/labels/bug": status(200)})
    async with runner_http(tmp_path, fake) as http:
        yes = await http.post(
            "/repos/labels/check", json={"owner": "acme-co", "name": "web", "label": "bug"}
        )
        no = await http.post(
            "/repos/labels/check", json={"owner": "acme-co", "name": "web", "label": "other"}
        )
        extra = await http.post(
            "/repos/labels/check",
            json={"owner": "acme-co", "name": "web", "label": "bug", "url": "https://x"},
        )
        bad = await http.post(
            "/repos/labels/check", json={"owner": "acme*", "name": "web", "label": "bug"}
        )
        unsafe = await http.post(
            "/repos/labels/check", json={"owner": "acme-co", "name": "web", "label": "a\nb"}
        )
    assert (yes.status_code, yes.json()) == (200, {"exists": True})
    assert no.json() == {"exists": False}
    assert extra.status_code == 400 and bad.status_code == 400 and unsafe.status_code == 400
    assert READ not in yes.text
    assert len(fake.seen) == 2


async def test_the_runner_answers_github_trouble_as_503_or_502(tmp_path: Path) -> None:
    body = {"owner": "acme-co", "name": "web", "label": "bug"}
    for code, want in ((429, 503), (500, 502)):
        fake = FakeGithub({"/repos/acme-co/web/labels/bug": status(code)})
        async with runner_http(tmp_path, fake) as http:
            assert (await http.post("/repos/labels/check", json=body)).status_code == want


async def test_the_execution_runner_has_no_label_check(tmp_path: Path) -> None:
    fake = FakeGithub({})
    async with runner_http(tmp_path, fake, execution=True) as http:
        answer = await http.post(
            "/repos/labels/check", json={"owner": "acme-co", "name": "web", "label": "bug"}
        )
    assert answer.status_code == 404 and fake.seen == []


async def test_the_client_posts_the_body_and_reads_the_answer_strictly() -> None:
    import json

    runner, seen = runner_client(200, {"exists": False})
    assert await runner.label_exists("acme-co", "web", LABEL) is False
    assert seen[0].method == "POST" and seen[0].url.path == "/repos/labels/check"
    assert json.loads(seen[0].content) == {"owner": "acme-co", "name": "web", "label": LABEL}
    for body in ({"exists": "no"}, {"exists": True, "more": 1}, {}):
        odd, _ = runner_client(200, body)
        with pytest.raises(RunnerError):
            await odd.label_exists("acme-co", "web", "bug")
    for code in (502, 503):
        busy, _ = runner_client(code, {"error": "x"})
        with pytest.raises(GithubBusy):
            await busy.label_exists("acme-co", "web", "bug")
    never, sent = runner_client(200, {"exists": True})
    for call_ in (
        never.label_exists("acme*", "web", "bug"),
        never.label_exists("acme-co", "web", " bug"),
    ):
        with pytest.raises(RunnerError):
            await call_
    assert sent == []


# --- the card -----------------------------------------------------------------------------


async def test_a_drawn_card_records_its_settings_and_a_redraw_shows_todays() -> None:
    import json

    from jotflow.orchestrator.card_sync import CardSync
    from jotflow.orchestrator.cards import CardPublisher
    from tests.test_card_sync import Clock, FakeCardPoster, FakePeople

    store = MemoryStore()
    await store.add_proposal(record())
    poster = FakeCardPoster()
    sync = CardSync(store, policy(SLACK), CardPublisher(store, poster), FakePeople(), clock=Clock())
    await sync.sync()
    found = await store.get_proposal(PID)
    assert found is not None and found.card_issues == SLACK.snapshot()
    assert "[Slack] Export" in json.dumps(poster.posts[0][2].blocks)
    # Not due, but redrawn on request, as approval asks when the settings changed.
    assert await sync.refresh(PID) is False
    assert await sync.redraw(PID) is True and len(poster.updates) == 1
    assert await sync.redraw("ffffffffffffffff") is False


async def test_an_approved_card_shows_what_the_approval_locked() -> None:
    from jotflow.orchestrator.card_sync import card_context
    from tests.test_card_sync import FakePeople

    store = MemoryStore()
    await store.add_proposal(record())
    await store.transition(
        PID, ProposalState.APPROVED, approved_by=AUTO_APPROVER, issues=SLACK.snapshot()
    )
    found = await store.get_proposal(PID)
    assert found is not None
    context = await card_context(policy(OLD), store, FakePeople(), found)
    assert context.issues == SLACK


async def test_a_failed_card_shows_todays_settings_so_retry_can_approve() -> None:
    """A Retry locks today's settings, so the failed card must show them (CodeRabbit)."""
    from jotflow.orchestrator.card_sync import card_context
    from tests.test_card_sync import FakePeople

    store = MemoryStore()
    added = await store.add_proposal(record())
    locked = content_hash(added.proposal)
    await store.transition(
        PID, ProposalState.APPROVED, approved_by=ALICE, expected_hash=locked, issues=OLD.snapshot()
    )
    await store.transition(PID, ProposalState.EXECUTING)
    failed = await store.transition(PID, ProposalState.FAILED, result=ExecutionResult(error="no"))
    context = await card_context(policy(SLACK), store, FakePeople(), failed)
    assert context.issues == SLACK
    # The redrawn failed card records today's settings, and Retry then approves.
    gate = Gate(SLACK)
    gate.store = store
    gate.handler = ApprovalHandler(store, policy(SLACK), gate.slack, redraw=gate._redraw)
    await store.set_card_ts(PID, CARD_TS)
    await store.mark_card_shown(PID, failed.revision, context.issues.snapshot())
    from jotflow.orchestrator.cards import ACTION_RETRY

    body = click(f"{PID}:{locked}")
    body["actions"][0]["action_id"] = ACTION_RETRY
    assert await gate.handler.on_approve(body) == "approved"
    assert (await gate.state()).approved_issues == SLACK.snapshot()


@pytest.mark.parametrize("label", [".", ".."])
async def test_a_dot_segment_label_is_never_looked_up(tmp_path: Path, label: str) -> None:
    """quote() leaves "." and "..", which a client resolves away (#347 review)."""
    fake = FakeGithub({"/repos/acme-co/web": status(200), "/repos/acme-co": status(200)})
    with pytest.raises(GithubError):
        await label_exists("acme-co", "web", label, READ, http=fake.client())
    async with runner_http(tmp_path, fake) as http:
        answer = await http.post(
            "/repos/labels/check", json={"owner": "acme-co", "name": "web", "label": label}
        )
    assert answer.status_code == 400
    runner, sent = runner_client(200, {"exists": True})
    with pytest.raises(RunnerError):
        await runner.label_exists("acme-co", "web", label)
    assert fake.seen == [] and sent == []
    # A label that merely contains dots is fine.
    from jotflow.policy import label_lookup_problem

    assert label_lookup_problem("v1.2") is None and label_lookup_problem("...") is None
