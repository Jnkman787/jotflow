"""Add me on a card and on a filed issue's reply, and its login dialog (#305)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jotflow.execution.writes import plan
from jotflow.models import (
    Assignable,
    ChannelMode,
    ExecutionResult,
    IssueProposal,
    Membership,
    ProposalRecord,
    ProposalState,
    RunKind,
    UpdateProposal,
)
from jotflow.orchestrator import add_me as m
from jotflow.orchestrator.add_me import AddMe, Submitted, Target, register_add_me
from jotflow.orchestrator.approval import NOT_ALLOWED, PAUSED, ApprovalHandler
from jotflow.orchestrator.cards import ACTION_ADD_ME, ACTION_ADD_ME_FILED, CardContext, render_card
from jotflow.orchestrator.keyword_handler import Person
from jotflow.orchestrator.launch import GithubBusy, RunnerError
from jotflow.orchestrator.modes import AutoApprover
from jotflow.orchestrator.thread_notice import reply_blocks, reply_text
from jotflow.policy import Policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore, SqliteStore, Store, content_hash
from jotflow.store.base import SELF_ADD_RUN
from tests.test_approval import ALICE, BOB, CARD_TS, CAROL, TEAM, Slack
from tests.test_runs import CH, START, THREAD

ALEX = ALICE
PID = "0123456789abcdef"
NEW = "fedcba9876543210"
REPLY_TS = "1760781800.000100"
URL = "https://github.com/acme-co/web/issues/42"
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}
ALEX_ID = 583231
CHRIS_ID = 777001


def policy(
    approvers: list[str] | None = None,
    *,
    org: str | None = None,
    repos: Any = None,
    second: bool = False,
) -> Policy:
    data: dict[str, Any] = {"channels": {CH: {"repos": repos or [WEB]}}, "approval": {}}
    if approvers is not None:
        data["approval"]["approvers"] = approvers
    if second:
        data["approval"]["require_second_approver"] = True
    if org is not None:
        data["identity"] = {"github_org": org}
    return Policy.model_validate(data)


class Checks:
    """The drafting runner's person checks, as a fake GitHub."""

    def __init__(self) -> None:
        self.accounts: dict[str, Assignable] = {
            "octo-alex": Assignable(account="user", id=ALEX_ID, login="octo-alex", assignable=True),
            "octo-chris": Assignable(
                account="user", id=CHRIS_ID, login="octo-chris", assignable=True
            ),
            "acme-co": Assignable(account="organization", id=9, login="acme-co"),
            "outsider": Assignable(account="user", id=5, login="outsider", assignable=False),
        }
        self.members: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail: Exception | None = None

    async def assignable(self, owner: str, name: str, login: str) -> Assignable:
        self.calls.append(("assignable", owner, name, login))
        if self.fail is not None:
            raise self.fail
        return self.accounts.get(login, Assignable(account="missing"))

    async def membership(self, org: str, login: str) -> Membership:
        self.calls.append(("membership", org, login))
        return Membership(member=self.members.get(login, "yes"))  # type: ignore[arg-type]


class AddMeSlack(Slack):
    def __init__(self) -> None:
        super().__init__()
        self.updated: list[tuple[str, Mapping[str, Any]]] = []

    async def update_modal(self, view_id: str, view: Mapping[str, Any]) -> None:
        self.updated.append((view_id, view))

    def shown(self) -> str:
        return str(self.updated[-1][1]["blocks"][0]["text"]["text"])


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> Any:
        return self.now


class Rig:
    def __init__(
        self,
        store: Store | None = None,
        *,
        approvers: list[str] | None = None,
        org: str | None = None,
        runner: bool = True,
        second: bool = False,
    ) -> None:
        self.store = store or MemoryStore()
        self.slack = AddMeSlack()
        self.checks = Checks()
        self.policy = policy(approvers, org=org, second=second)
        self.refreshed: list[str] = []
        self.clock = Clock()
        self.seconds = 0.0
        self.ids = iter(f"{n:016x}" for n in range(1, 100))
        self.gate = ApprovalHandler(self.store, self.policy, self.slack)
        self.add_me = AddMe(
            self.store,
            self.policy,
            self.gate,
            self.slack,
            (lambda: self.checks) if runner else (lambda: None),
            refresh=self._refresh,
            clock=self.clock,
            monotonic=lambda: self.seconds,
            new_id=lambda: next(self.ids),
        )

    async def _refresh(self, proposal_id: str) -> None:
        self.refreshed.append(proposal_id)

    async def card(self, state: ProposalState = ProposalState.PENDING) -> None:
        draft = IssueProposal(repo="acme-co/web", title="Export", body="CSV.", confidence=0.9)
        record = ProposalRecord(
            id=PID,
            channel_id=CH,
            thread_ts=THREAD,
            run_id="draft-run",
            proposal=draft,
            participants=[BOB],
            created_at=START,
        )
        await self.store.add_proposal(record)
        await self.store.set_card_ts(PID, CARD_TS)
        if state is ProposalState.FAILED:
            await self._approve_and_run(PID, draft)
            await self.store.transition(
                PID, ProposalState.FAILED, result=ExecutionResult(error="no")
            )

    async def filed(self, *, topic: bool = True) -> None:
        topic_id = (await self.store.create_topic(CH, "Export")).topic.id if topic else None
        draft = IssueProposal(
            topic_id=topic_id, repo="acme-co/web", title="Export", body="CSV.", confidence=0.9
        )
        record = ProposalRecord(
            id=PID,
            channel_id=CH,
            thread_ts=THREAD,
            run_id="draft-run",
            proposal=draft,
            created_at=START,
        )
        await self.store.add_proposal(record)
        await self._approve_and_run(PID, draft)
        await self.store.transition(
            PID, ProposalState.APPLIED, result=ExecutionResult(issue_url=URL)
        )

    async def _approve_and_run(self, proposal_id: str, draft: Any) -> None:
        await self.store.transition(
            proposal_id,
            ProposalState.APPROVED,
            approved_by=CAROL,
            expected_hash=content_hash(draft),
        )
        await self.store.transition(proposal_id, ProposalState.EXECUTING)

    async def self_adds(self) -> list[ProposalRecord]:
        return [
            r for r in await self.store.thread_proposals(CH, THREAD) if r.run_id == SELF_ADD_RUN
        ]

    async def executions(self) -> int:
        runs = await self.store.thread_runs(CH, THREAD)
        return sum(1 for run in runs if run.kind is RunKind.EXECUTION and run.proposal_id != PID)

    async def submit(self, login: str, target: Target, *, user: str = ALEX) -> str:
        checked = await self.add_me.check_submitted(submission(login, target, user=user))
        if isinstance(checked, str):
            return checked
        return await self.add_me.finish_submitted(checked)


def click_body(
    action_id: str,
    value: str,
    *,
    user: str = ALEX,
    team: str = TEAM,
    ts: str = CARD_TS,
    thread: str | None = None,
) -> dict[str, Any]:
    container: dict[str, Any] = {"type": "message", "message_ts": ts, "channel_id": CH}
    if thread is not None:
        container["thread_ts"] = thread
    return {
        "type": "block_actions",
        "user": {"id": user, "team_id": team},
        "team": {"id": team},
        "channel": {"id": CH},
        "container": container,
        "trigger_id": "1.2.trigger",
        "actions": [{"action_id": action_id, "type": "button", "value": value}],
    }


def card_click(**kw: Any) -> dict[str, Any]:
    return click_body(ACTION_ADD_ME, f"{PID}:me", **kw)


def filed_click(**kw: Any) -> dict[str, Any]:
    kw.setdefault("thread", THREAD)
    kw.setdefault("ts", REPLY_TS)
    return click_body(ACTION_ADD_ME_FILED, PID, **kw)


def submission(login: str, target: Target, *, user: str = ALEX) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "user": {"id": user, "team_id": TEAM},
        "team": {"id": TEAM},
        "view": {
            "id": "V0DIALOG",
            "private_metadata": target.encode(),
            "state": {"values": {m.LOGIN_BLOCK: {m.LOGIN_INPUT: {"value": login}}}},
        },
    }


CARD = Target("card", PID, CH, CARD_TS)
FILED = Target("filed", PID, CH, THREAD)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


async def _draft(rig: Rig) -> IssueProposal:
    record = await rig.store.get_proposal(PID)
    assert record is not None and isinstance(record.proposal, IssueProposal)
    return record.proposal


# --- on a card ----------------------------------------------------------------------


@pytest.mark.parametrize("approvers", [None, [CAROL]])
async def test_a_linked_person_adds_themselves_on_the_card(approvers: list[str] | None) -> None:
    rig = Rig(approvers=approvers)
    await rig.card()
    await rig.store.link_identity(ALEX, "octo-alex")
    assert await rig.add_me.on_card(card_click()) == "added"
    draft = await _draft(rig)
    assert draft.assignees == ["octo-alex"]
    assert [(r.login, r.why) for r in draft.assignee_reasons] == [("octo-alex", "added_on_card")]
    (edit,) = await rig.store.proposal_edits(PID)
    assert (edit.edited_by, edit.field) == (ALEX, "assign")
    assert rig.refreshed == [PID]
    assert rig.slack.ephemerals == [(CH, ALEX, "Added octo-alex as an assignee on the card.")]
    assert rig.checks.calls == []  # a person approves the card, so GitHub isn't asked


async def test_adding_yourself_again_changes_nothing() -> None:
    rig = Rig()
    await rig.card()
    await rig.store.link_identity(ALEX, "octo-alex")
    await rig.add_me.on_card(card_click())
    assert await rig.add_me.on_card(card_click()) == "unchanged"
    assert len(await rig.store.proposal_edits(PID)) == 1


async def test_a_failed_card_takes_add_me_too() -> None:
    rig = Rig()
    await rig.card(ProposalState.FAILED)
    await rig.store.link_identity(ALEX, "octo-alex")
    assert await rig.add_me.on_card(card_click()) == "added"


async def test_a_card_with_ten_assignees_takes_no_more() -> None:
    rig = Rig()
    await rig.card()
    record = await rig.store.get_proposal(PID)
    assert record is not None
    full = IssueProposal.model_validate(
        {**record.proposal.model_dump(), "assignees": [f"octo-{n}" for n in range(10)]}
    )
    await rig.store.edit_proposal(PID, full, by=BOB, field="assign")
    await rig.store.link_identity(ALEX, "octo-alex")
    assert await rig.add_me.on_card(card_click()) == "refused"
    assert rig.slack.ephemerals[-1] == (CH, ALEX, m.TOO_MANY_ASSIGNEES)


async def test_guests_and_paused_channels_are_refused_on_the_card() -> None:
    rig = Rig()
    await rig.card()
    await rig.store.link_identity(ALEX, "octo-alex")
    rig.slack.people[ALEX] = Person(ALEX, guest=True)
    assert await rig.add_me.on_card(card_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == NOT_ALLOWED
    rig.slack.people.clear()
    await rig.store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by=CAROL)
    assert await rig.add_me.on_card(card_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == PAUSED
    assert (await _draft(rig)).assignees == []


async def test_an_unlinked_person_gets_the_dialog_and_is_added_once_github_agrees() -> None:
    rig = Rig()
    await rig.card()
    assert await rig.add_me.on_card(card_click()) == "asked"
    ((trigger, view),) = rig.slack.modals
    assert trigger == "1.2.trigger" and view["callback_id"] == m.CALLBACK_ADD_ME
    assert Target.decode(view["private_metadata"]) == CARD
    text = await rig.submit("@octo-alex", CARD)
    assert text.startswith("Linked your Slack account to GitHub login octo-alex, unverified.")
    assert text.endswith("Added octo-alex as an assignee on the card.")
    assert rig.slack.updated[-1][0] == "V0DIALOG"
    link = await rig.store.get_identity(ALEX)
    assert link is not None and not link.verified and link.linked_by == "self"
    assert (await _draft(rig)).assignees == ["octo-alex"]
    assert rig.checks.calls == [("assignable", "acme-co", "web", "octo-alex")]


@pytest.mark.parametrize(
    ("login", "error"),
    [
        ("not a login!", m.NOT_A_LOGIN),
        ("ghost", "There's no GitHub account named ghost."),
        ("acme-co", "acme-co is a GitHub organization, not a person."),
        (
            "outsider",
            "outsider can't be assigned issues in acme-co/web. Ask a maintainer to add them "
            "to the organization or the repo.",
        ),
    ],
)
async def test_the_dialog_names_each_problem(login: str, error: str) -> None:
    rig = Rig()
    await rig.card()
    assert await rig.submit(login, CARD) == error
    assert await rig.store.get_identity(ALEX) is None
    assert (await _draft(rig)).assignees == []


async def test_a_login_someone_else_linked_points_to_verify() -> None:
    rig = Rig()
    await rig.card()
    await rig.store.link_identity(BOB, "octo-alex")
    assert await rig.submit("octo-alex", CARD) == m.TAKEN
    assert rig.checks.calls == []


@pytest.mark.parametrize(
    ("failure", "error"),
    [(GithubBusy("busy"), m.BUSY), (RunnerError("down"), m.UNAVAILABLE)],
)
async def test_github_failing_is_never_no_such_account(failure: Exception, error: str) -> None:
    rig = Rig()
    await rig.card()
    rig.checks.fail = failure
    assert await rig.submit("octo-alex", CARD) == error
    assert await rig.store.get_identity(ALEX) is None


async def test_without_the_drafting_runner_nothing_is_checked() -> None:
    rig = Rig(runner=False)
    await rig.card()
    assert await rig.submit("octo-alex", CARD) == m.NO_RUNNER


@pytest.mark.parametrize(
    ("member", "error"),
    [
        ("no", "octo-alex isn't a member of the acme-co organization on GitHub."),
        (
            "hidden",
            "JotFlow can't see whether octo-alex is a member of acme-co. Whoever runs JotFlow "
            "needs to give its read token the organization's Members: Read-only permission.",
        ),
    ],
)
async def test_an_org_check_fails_closed(member: str, error: str) -> None:
    rig = Rig(org="acme-co")
    await rig.card()
    rig.checks.members["octo-alex"] = member
    assert await rig.submit("octo-alex", CARD) == error
    assert rig.checks.calls[-1] == ("membership", "acme-co", "octo-alex")


async def test_an_org_member_is_added() -> None:
    rig = Rig(org="acme-co")
    await rig.card()
    assert (await rig.submit("octo-alex", CARD)).endswith("on the card.")


async def test_the_checks_are_limited_per_person() -> None:
    rig = Rig()
    await rig.card()
    for _ in range(m.MAX_CHECKS):
        await rig.submit("ghost", CARD)
    assert await rig.submit("ghost", CARD) == m.TOO_MANY_CHECKS
    assert len(rig.checks.calls) == m.MAX_CHECKS
    # Someone else has their own allowance, and the window moves on.
    assert (await rig.submit("ghost", CARD, user=BOB)).startswith("There's no")
    rig.seconds += m.CHECK_WINDOW_SECONDS
    assert (await rig.submit("ghost", CARD)).startswith("There's no")


async def test_an_org_check_counts_twice() -> None:
    rig = Rig(org="acme-co")
    await rig.card()
    for _ in range(m.MAX_CHECKS // 2):
        await rig.submit("ghost", CARD)
    assert await rig.submit("ghost", CARD) == m.TOO_MANY_CHECKS


async def test_the_dialog_rechecks_who_and_where() -> None:
    rig = Rig()
    await rig.card()
    rig.slack.people[ALEX] = Person(ALEX, external=True)
    assert await rig.submit("octo-alex", CARD) == NOT_ALLOWED
    rig.slack.people.clear()
    assert await rig.submit("octo-alex", Target("card", PID, CH, "1.2")) == m.NOT_EDITABLE
    await rig.store.transition(PID, ProposalState.REJECTED)
    assert await rig.submit("octo-alex", CARD) == m.NOT_EDITABLE


async def test_someone_who_linked_meanwhile_clicks_again() -> None:
    rig = Rig()
    await rig.card()
    await rig.store.link_identity(ALEX, "octo-alex")
    assert await rig.submit("octo-alex", CARD) == m.ALREADY_LINKED


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        '["card"]',
        '["other", "0123456789abcdef", "C0IDEAS01", "1.2"]',
        '["card", "XYZ", "C0IDEAS01", "1.2"]',
        '["card", "0123456789abcdef", "nope", "1.2"]',
        '["card", "0123456789abcdef", "C0IDEAS01", "soon"]',
    ],
)
async def test_a_dialog_with_strange_metadata_is_refused(raw: Any) -> None:
    assert Target.decode(raw) is None
    rig = Rig()
    body = submission("octo-alex", CARD)
    body["view"]["private_metadata"] = raw
    assert await rig.add_me.check_submitted(body) == m.NOT_EDITABLE


# --- on a filed issue's reply -------------------------------------------------------


async def test_a_verified_person_in_open_mode_is_added_at_once(store: Store) -> None:
    rig = Rig(store)
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    assert await rig.add_me.on_filed(filed_click()) == "filing"
    (added,) = await rig.self_adds()
    assert added.state is ProposalState.APPROVED and added.approved_by == ALEX
    assert added.participants == [ALEX]
    draft = added.proposal
    assert isinstance(draft, UpdateProposal)
    assert (draft.repo, draft.issue_number) == ("acme-co/web", 42)
    assert draft.changes.add_assignees == ["octo-alex"]
    assert added.content_hash == content_hash(draft)
    # Exactly one planned write, adding that login.
    _, writes = plan(added, "")
    (write,) = writes
    assert write.tool == "issue_write" and write.assignees_from_current is not None
    assert write.assignees_from_current.add == ["octo-alex"]
    assert "body" not in write.arguments
    assert await rig.executions() == 1
    assert rig.slack.ephemerals == [(CH, ALEX, "Adding you as an assignee of web#42 now.")]


async def test_an_approver_in_approvers_mode_is_added_at_once() -> None:
    rig = Rig(approvers=[ALEX])
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    assert await rig.add_me.on_filed(filed_click()) == "filing"


@pytest.mark.parametrize("case", ["self-declared", "other account", "not an approver"])
async def test_otherwise_the_add_goes_on_a_card(case: str) -> None:
    rig = Rig(approvers=[CAROL] if case == "not an approver" else None)
    await rig.filed()
    if case == "self-declared":
        await rig.store.link_identity(ALEX, "octo-alex")
    else:
        # An admin link whose stored account id isn't the one GitHub gives now.
        github_id = ALEX_ID + 1 if case == "other account" else ALEX_ID
        await rig.store.admin_link(ALEX, "octo-alex", github_id)
    assert await rig.add_me.on_filed(filed_click()) == "carded"
    (added,) = await rig.self_adds()
    assert added.state is ProposalState.PENDING and added.approved_by is None
    assert await rig.executions() == 0
    assert rig.slack.ephemerals[-1][2].startswith("Asked to add you to web#42.")


async def test_a_login_entered_for_someone_else_never_files(store: Store) -> None:
    """Alex enters Chris's login and clicks Add me on a filed issue (decision 7)."""
    rig = Rig(store)
    await rig.filed()
    assert await rig.add_me.on_filed(filed_click()) == "asked"
    assert Target.decode(rig.slack.modals[0][1]["private_metadata"]) == FILED
    text = await rig.submit("octo-chris", FILED)
    assert "A card in the thread waits for an approver." in text
    (added,) = await rig.self_adds()
    assert added.state is ProposalState.PENDING
    assert await rig.executions() == 0
    # The card says whose claim the login is.
    card = render_card(
        added,
        CardContext(
            channel=rig.policy.channel(CH),
            names={ALEX: "Alex"},
            identities={ALEX: await _link(rig, ALEX)},
        ),
    )
    lines = _texts(card.blocks)
    assert (
        "Alex asked to be added with Add me. Alex says octo-chris is their GitHub account "
        "(unverified)." in lines
    )
    assert not any(line.startswith("Confidence") for line in lines)
    # Auto mode never approves it either.
    await rig.store.set_channel_mode(CH, ChannelMode.AUTO, changed_by=CAROL)
    auto = AutoApprover(rig.store, _mode(rig.store))
    assert await auto.approve_due() == 0


async def test_a_trusted_persons_card_has_no_claim_line() -> None:
    rig = Rig(approvers=[CAROL])
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    await rig.add_me.on_filed(filed_click())
    (added,) = await rig.self_adds()
    card = render_card(
        added,
        CardContext(
            channel=rig.policy.channel(CH),
            names={ALEX: "Alex"},
            identities={ALEX: await _link(rig, ALEX)},
        ),
    )
    assert "Alex asked to be added with Add me." in _texts(card.blocks)


async def test_one_waiting_request_per_issue() -> None:
    rig = Rig(approvers=[CAROL])
    await rig.filed()
    await rig.store.link_identity(ALEX, "octo-alex")
    await rig.add_me.on_filed(filed_click())
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == (
        "You've already asked to be added to web#42. Its card is in the thread."
    )
    assert len(await rig.self_adds()) == 1


async def test_five_requests_an_hour(store: Store) -> None:
    rig = Rig(store)
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    for _ in range(m.MAX_SELF_ADDS):
        assert await rig.add_me.on_filed(filed_click()) == "filing"
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == m.TOO_MANY_ADDS
    assert await rig.store.count_self_adds(ALEX, START) == m.MAX_SELF_ADDS
    assert await rig.store.count_self_adds(BOB, START) == 0
    rig.clock.now = START + m.SELF_ADD_WINDOW + timedelta(seconds=1)
    assert await rig.add_me.on_filed(filed_click()) == "filing"


async def test_a_new_draft_never_supersedes_a_waiting_request(store: Store) -> None:
    rig = Rig(store, approvers=[CAROL])
    await rig.filed()
    await rig.store.link_identity(ALEX, "octo-alex")
    await rig.add_me.on_filed(filed_click())
    (added,) = await rig.self_adds()
    assert await rig.store.supersede_pending(CH, THREAD, keep_id="other") == []
    current = await rig.store.get_proposal(added.id)
    assert current is not None and current.state is ProposalState.PENDING


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"thread": "1760781600.999999"}, m.NOT_THIS_REPLY),
        ({"thread": None}, m.NOT_THIS_REPLY),
    ],
)
async def test_the_reply_must_be_in_the_issues_own_thread(
    change: dict[str, Any], error: str
) -> None:
    rig = Rig()
    await rig.filed()
    await rig.store.link_identity(ALEX, "octo-alex")
    assert await rig.add_me.on_filed(filed_click(**change)) == "refused"
    assert rig.slack.ephemerals[-1][2] == error


async def test_a_reply_value_that_is_not_a_proposal_is_refused() -> None:
    rig = Rig()
    await rig.filed()
    body = click_body(ACTION_ADD_ME_FILED, "nope", ts=REPLY_TS, thread=THREAD)
    assert await rig.add_me.on_filed(body) == "refused"
    assert rig.slack.ephemerals[-1][2] == m.NOT_THIS_REPLY


async def test_an_unfiled_proposal_uses_its_card() -> None:
    rig = Rig()
    await rig.card()
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == m.NOT_FILED


async def test_an_issue_without_a_topic_is_refused() -> None:
    rig = Rig()
    await rig.filed(topic=False)
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == m.NO_TOPIC


async def test_a_repo_the_channel_no_longer_feeds_is_refused() -> None:
    rig = Rig()
    await rig.filed()
    other = {"repo": "acme-co/api", "nickname": "api", "description": "API"}
    rig.add_me._repos = type(rig.add_me._repos).static(policy(repos=[other]))
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == m.REPO_GONE


async def test_a_paused_channel_or_an_unlisted_one_refuses_the_reply() -> None:
    rig = Rig()
    await rig.filed()
    await rig.store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by=CAROL)
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == PAUSED
    body = filed_click()
    body["channel"]["id"] = "C0ELSEWHERE"
    told = len(rig.slack.ephemerals)
    assert await rig.add_me.on_filed(body) == "refused"
    assert len(rig.slack.ephemerals) == told  # never a reply in a channel JotFlow doesn't feed


async def test_a_malformed_reply_click_is_ignored() -> None:
    rig = Rig()
    body = filed_click()
    del body["container"]["message_ts"]
    assert await rig.add_me.on_filed(body) == "ignored"


async def test_a_filed_add_needs_github_to_agree() -> None:
    rig = Rig()
    await rig.filed()
    await rig.store.link_identity(ALEX, "outsider")
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2].startswith("outsider can't be assigned issues")
    assert await rig.self_adds() == []


async def test_the_filed_dialog_rechecks_the_cap_before_github() -> None:
    rig = Rig(approvers=[CAROL])
    await rig.filed()
    await rig.store.link_identity(BOB, "octo-bob")
    for n in range(m.MAX_SELF_ADDS):
        await rig.store.add_proposal(
            ProposalRecord(
                id=f"{n + 50:016x}",
                channel_id=CH,
                thread_ts=f"1760781600.00020{n}",
                run_id=SELF_ADD_RUN,
                proposal=UpdateProposal(
                    topic_id=(await rig.store.create_topic(CH, f"T{n}")).topic.id,
                    repo="acme-co/web",
                    issue_number=7,
                    changes={"add_assignees": ["octo-alex"]},  # type: ignore[arg-type]
                    confidence=1.0,
                ),
                participants=[ALEX],
                created_at=START,
            )
        )
    assert await rig.submit("octo-alex", FILED) == m.TOO_MANY_ADDS
    assert rig.checks.calls == []


async def test_two_clicks_at_once_make_one_request() -> None:
    import asyncio

    rig = Rig(approvers=[CAROL])
    await rig.filed()
    await rig.store.link_identity(ALEX, "octo-alex")
    outcomes = await asyncio.gather(
        rig.add_me.on_filed(filed_click()), rig.add_me.on_filed(filed_click())
    )
    assert sorted(outcomes) == ["carded", "refused"]
    assert len(await rig.self_adds()) == 1


async def test_an_unexpected_failure_still_answers_the_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    await rig.card()
    checked = await rig.add_me.check_submitted(submission("octo-alex", CARD))
    assert isinstance(checked, Submitted)

    async def broken(submitted: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(rig.add_me, "_finish", broken)
    assert await rig.add_me.finish_submitted(checked) == m.FAILED
    assert rig.slack.shown() == m.FAILED


# --- the reply's blocks -------------------------------------------------------------


async def test_the_filed_reply_keeps_its_text_and_gains_add_me() -> None:
    rig = Rig()
    await rig.filed()
    record = await rig.store.get_proposal(PID)
    assert record is not None
    text = reply_text(rig.policy, record)
    assert text == f"Created <{URL}|web#42>"  # what jotflow rebuild reads (#38)
    section, actions = reply_blocks(text, PID)
    assert section["text"] == {"type": "mrkdwn", "text": text}
    (button,) = actions["elements"]
    assert (button["action_id"], button["value"]) == (ACTION_ADD_ME_FILED, PID)


def test_pending_and_failed_cards_show_add_me() -> None:
    record = ProposalRecord(
        id=PID,
        channel_id=CH,
        thread_ts=THREAD,
        run_id="draft-run",
        proposal=IssueProposal(repo="acme-co/web", title="T", body="B", confidence=0.9),
        created_at=START,
    )
    context = CardContext(channel=policy().channel(CH))
    for state in (ProposalState.PENDING, ProposalState.FAILED):
        card = render_card(record.model_copy(update={"state": state}), context)
        buttons = [
            (e["action_id"], e["value"])
            for b in card.blocks
            if b["type"] == "actions"
            for e in b["elements"]
        ]
        assert (ACTION_ADD_ME, f"{PID}:me") in buttons


def test_github_org_round_trips_through_the_writer() -> None:
    written = render_policy(policy(org="acme-co"))
    assert 'github_org = "acme-co"' in written
    assert "github_org" not in render_policy(policy())


# --- through Bolt -------------------------------------------------------------------


class FakeApp:
    def __init__(self) -> None:
        self.actions: dict[str, Any] = {}
        self.views: dict[str, Any] = {}

    def action(self, name: str) -> Any:
        return lambda handler: self.actions.setdefault(name, handler)

    def view(self, name: str) -> Any:
        return lambda handler: self.views.setdefault(name, handler)


class Ack:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def test_bolt_acks_errors_in_the_dialog_and_updates_it_after_github() -> None:
    rig = Rig()
    await rig.card()
    app = FakeApp()
    register_add_me(app, rig.add_me)
    assert set(app.actions) == {ACTION_ADD_ME, ACTION_ADD_ME_FILED}
    ack = Ack()
    await app.views[m.CALLBACK_ADD_ME](ack, submission("not a login!", CARD))
    assert ack.calls == [{"response_action": "errors", "errors": {m.LOGIN_BLOCK: m.NOT_A_LOGIN}}]
    ack = Ack()
    await app.views[m.CALLBACK_ADD_ME](ack, submission("octo-alex", CARD))
    assert ack.calls == [{"response_action": "update", "view": m.result_modal(m.CHECKING)}]
    assert rig.slack.shown().endswith("Added octo-alex as an assignee on the card.")
    ack = Ack()
    await app.actions[ACTION_ADD_ME](ack, card_click(user=BOB))
    assert ack.calls == [{}] and rig.slack.modals  # Bob isn't linked: the dialog opens
    ack = Ack()
    await app.actions[ACTION_ADD_ME_FILED](ack, filed_click())
    assert ack.calls == [{}]


async def test_a_failing_check_still_answers_the_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig()
    app = FakeApp()
    register_add_me(app, rig.add_me)

    async def broken(body: Any) -> Submitted | str:
        raise RuntimeError("boom")

    monkeypatch.setattr(rig.add_me, "check_submitted", broken)
    ack = Ack()
    await app.views[m.CALLBACK_ADD_ME](ack, submission("octo-alex", CARD))
    assert ack.calls[0]["response_action"] == "errors"


# --- helpers ------------------------------------------------------------------------


async def _link(rig: Rig, user: str) -> Any:
    link = await rig.store.get_identity(user)
    assert link is not None
    return link


def _texts(blocks: list[dict[str, Any]]) -> list[str]:
    found = []
    for block in blocks:
        if block["type"] == "context":
            found += [e["text"] for e in block["elements"]]
        elif block["type"] == "section":
            found.append(block["text"]["text"])
    return found


def _mode(store: Store) -> Any:
    async def mode_for(channel_id: str) -> ChannelMode:
        setting = await store.get_channel_mode(channel_id)
        return ChannelMode.PROPOSE if setting is None else setting.mode

    return mode_for


# --- #344 review: a second approver, and one cap for both kinds of Add me ------------


async def test_with_a_second_approver_required_a_filed_add_always_waits_on_a_card() -> None:
    # Even a trusted approver's own request: someone else must approve it.
    rig = Rig(approvers=[ALEX, CAROL], second=True)
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    assert await rig.add_me.on_filed(filed_click()) == "carded"
    (added,) = await rig.self_adds()
    assert added.state is ProposalState.PENDING and await rig.executions() == 0


async def test_the_requester_cant_approve_their_own_add_me_card() -> None:
    from jotflow.orchestrator.approval import OWN_PROPOSAL
    from jotflow.orchestrator.cards import ACTION_APPROVE

    rig = Rig(approvers=[ALEX, CAROL], second=True)
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    await rig.add_me.on_filed(filed_click())
    (added,) = await rig.self_adds()
    await rig.store.set_card_ts(added.id, "1760781800.000100")
    value = f"{added.id}:{content_hash(added.proposal)}"
    own = click_body(ACTION_APPROVE, value, user=ALEX, ts="1760781800.000100")
    assert await rig.gate.on_approve(own) == "refused"
    assert rig.slack.ephemerals[-1][2] == OWN_PROPOSAL
    other = click_body(ACTION_APPROVE, value, user=CAROL, ts="1760781800.000100")
    assert await rig.gate.on_approve(other) == "approved"


async def test_add_me_on_cards_counts_toward_the_same_hourly_cap() -> None:
    rig = Rig()
    await rig.filed()
    await rig.store.admin_link(ALEX, "octo-alex", ALEX_ID)
    for _ in range(m.MAX_SELF_ADDS - 1):
        assert await rig.add_me.on_filed(filed_click()) == "filing"
    # A pending card elsewhere in the channel: the fifth request, on a card.
    other = "00000000000000aa"
    draft = IssueProposal(repo="acme-co/web", title="Other", body="X.", confidence=0.9)
    await rig.store.add_proposal(
        ProposalRecord(
            id=other,
            channel_id=CH,
            thread_ts="1760781900.000100",
            run_id="draft-run-2",
            proposal=draft,
            created_at=START,
        )
    )
    await rig.store.set_card_ts(other, "1760781900.000200")
    card = click_body(ACTION_ADD_ME, f"{other}:me", ts="1760781900.000200")
    assert await rig.add_me.on_card(card) == "added"
    assert await rig.add_me.on_filed(filed_click()) == "refused"
    assert rig.slack.ephemerals[-1][2] == m.TOO_MANY_ADDS
    rig.clock.now = START + m.SELF_ADD_WINDOW + timedelta(seconds=1)
    assert await rig.add_me.on_filed(filed_click()) == "filing"
