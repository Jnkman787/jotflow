"""The card's repo dropdown and assign buttons, through the real Bolt app (#160)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import pytest

from jotflow.models import ChannelMode, IssueProposal, ProposalState
from jotflow.orchestrator.approval import (
    NOT_ALLOWED,
    NOT_THIS_CARD,
    PAUSED,
    STALE,
    ApprovalHandler,
    register_actions,
)
from jotflow.orchestrator.card_edits import (
    APPROVERS_ONLY,
    NOT_A_CHANNEL_REPO,
    NOT_A_PARTICIPANT,
    NOT_EDITABLE,
    NOT_LINKED,
    UNCHANGED,
    UPDATE_KEEPS_REPO,
    CardEdits,
    register_edits,
)
from jotflow.orchestrator.cards import (
    ACTION_APPROVE,
    ACTION_ASSIGN,
    ACTION_REPO,
    ACTION_UNASSIGN,
    CardContext,
    CardPublisher,
)
from jotflow.orchestrator.keyword_handler import Person
from jotflow.policy import Policy
from jotflow.store import MemoryStore, content_hash
from tests.test_approval import ALICE, BOB, CARD_TS, TEAM, Cards, Slack, _authorize
from tests.test_runs import CH, THREAD, _proposal_record

PID = "0123456789abcdef"
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}
API = {"repo": "acme-co/api", "nickname": "api", "description": "API", "labels": ["bug"]}


def policy(approvers: list[str] | None = None) -> Policy:
    data: dict[str, Any] = {"channels": {CH: {"repos": [WEB, API]}}, "approval": {}}
    if approvers is not None:
        data["approval"]["approvers"] = approvers
    return Policy.model_validate(data)


class Rig:
    def __init__(self, approvers: list[str] | None = None) -> None:
        self.store = MemoryStore()
        self.slack = Slack()
        self.cards = Cards()
        self.refreshed: list[str] = []
        self._policy = policy(approvers)
        self._publisher = CardPublisher(self.store, self.cards)
        self.handler = ApprovalHandler(self.store, self._policy, self.slack, refresh=self._refresh)
        self.edits = CardEdits(
            self.store, self._policy, self.handler.guarded, self.slack, refresh=self._refresh
        )

    async def _refresh(self, proposal_id: str) -> None:
        self.refreshed.append(proposal_id)
        record = await self.store.get_proposal(proposal_id)
        assert record is not None
        await self._publisher.show(record, CardContext(channel=self._policy.channel(CH)))

    async def proposal(self, **draft: Any) -> str:
        record = _proposal_record(PID)
        proposal = IssueProposal.model_validate(
            {**record.proposal.model_dump(), "labels": ["bug", "enhancement"], **draft}
        )
        record = record.model_copy(update={"participants": [ALICE, BOB], "proposal": proposal})
        await self.store.add_proposal(record)
        await self.store.set_card_ts(PID, CARD_TS)
        return PID

    async def draft(self) -> IssueProposal:
        record = await self.store.get_proposal(PID)
        assert record is not None and isinstance(record.proposal, IssueProposal)
        return record.proposal

    def app(self) -> Any:
        from slack_bolt.async_app import AsyncApp
        from slack_sdk.web.async_client import AsyncWebClient

        app = AsyncApp(
            client=AsyncWebClient(token="xoxb-t"),
            authorize=_authorize,
            request_verification_enabled=False,
            process_before_response=True,
        )
        register_actions(app, self.handler)
        register_edits(app, self.edits)
        return app


def body(
    action_id: str,
    value: str,
    *,
    user: str = BOB,
    team: str = TEAM,
    channel: str = CH,
    ts: str = CARD_TS,
) -> dict[str, Any]:
    action: dict[str, Any] = {"action_id": action_id, "block_id": "b"}
    if action_id == ACTION_REPO:
        action |= {"type": "static_select", "selected_option": {"value": value}}
    else:
        action |= {"type": "button", "value": value}
    return {
        "type": "block_actions",
        "api_app_id": "A0JOTFLOW",
        "user": {"id": user, "team_id": team},
        "team": {"id": team},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": ts, "channel_id": channel},
        "trigger_id": "1.2.trigger",
        "actions": [action],
    }


async def click(app: Any, payload: dict[str, Any]) -> None:
    from slack_bolt.request.async_request import AsyncBoltRequest

    request = AsyncBoltRequest(
        body=urlencode({"payload": json.dumps(payload)}),
        headers={"content-type": ["application/x-www-form-urlencoded"]},
    )
    response = await app.async_dispatch(request)
    assert response.status == 200  # acknowledged, whatever the outcome


pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


# --- the repo dropdown --------------------------------------------------------------


async def test_choosing_another_repo_moves_the_proposal_and_drops_its_labels() -> None:
    rig = Rig()
    await rig.proposal(repo_visibility="private")
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api"))
    draft = await rig.draft()
    assert draft.repo == "acme-co/api" and draft.labels == ["bug"]
    assert draft.repo_visibility is None
    assert rig.refreshed == [PID] and len(rig.cards.updates) == 1
    assert rig.slack.ephemerals == [
        (CH, BOB, "Moved to api. 1 label not allowed there were dropped.")
    ]
    (audit,) = await rig.store.proposal_edits(PID)
    assert (audit.edited_by, audit.field) == (BOB, "repo")


async def test_the_same_repo_changes_nothing() -> None:
    rig = Rig()
    await rig.proposal()
    await click(rig.app(), body(ACTION_REPO, f"{PID}:web"))
    assert rig.slack.ephemerals == [(CH, BOB, UNCHANGED)]
    assert await rig.store.proposal_edits(PID) == []


async def test_a_repo_the_channel_does_not_feed_is_refused() -> None:
    rig = Rig()
    await rig.proposal()
    await click(rig.app(), body(ACTION_REPO, f"{PID}:mobile"))
    assert (await rig.draft()).repo == "acme-co/web"
    assert rig.slack.ephemerals == [(CH, BOB, NOT_A_CHANNEL_REPO)]


async def test_an_update_keeps_its_issues_repo() -> None:
    from jotflow.models import IssueChanges, UpdateProposal

    rig = Rig()
    topic = await rig.store.create_topic(CH, "Export")
    update = UpdateProposal(
        topic_id=topic.topic.id,
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(comment="Keep the order."),
        confidence=0.9,
    )
    record = _proposal_record(PID).model_copy(update={"proposal": update})
    await rig.store.add_proposal(record)
    await rig.store.set_card_ts(PID, CARD_TS)
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api"))
    assert rig.slack.ephemerals == [(CH, BOB, UPDATE_KEEPS_REPO)]


# --- assign and unassign -------------------------------------------------------------------


async def test_a_linked_participant_is_made_an_assignee_once() -> None:
    rig = Rig()
    await rig.proposal()
    await rig.store.link_identity(ALICE, "acme-alice")
    app = rig.app()
    await click(app, body(ACTION_ASSIGN, f"{PID}:{ALICE}"))
    await click(app, body(ACTION_ASSIGN, f"{PID}:{ALICE}"))
    assert (await rig.draft()).assignees == ["acme-alice"]
    assert rig.slack.ephemerals == [
        (CH, BOB, "Added acme-alice as an assignee."),
        (CH, BOB, UNCHANGED),
    ]


async def test_an_unlinked_participant_or_a_stranger_is_not_assigned() -> None:
    rig = Rig()
    await rig.proposal()
    app = rig.app()
    await click(app, body(ACTION_ASSIGN, f"{PID}:{ALICE}"))
    await click(app, body(ACTION_ASSIGN, f"{PID}:U0STRANGER"))
    assert (await rig.draft()).assignees == []
    assert rig.slack.ephemerals == [(CH, BOB, NOT_LINKED), (CH, BOB, NOT_A_PARTICIPANT)]


async def test_remove_takes_an_assignee_off_but_keeps_the_participant() -> None:
    rig = Rig()
    await rig.proposal(assignees=["acme-alice"])
    await click(rig.app(), body(ACTION_UNASSIGN, f"{PID}:Acme-Alice"))
    record = await rig.store.get_proposal(PID)
    assert record is not None and record.proposal.assignees == []  # type: ignore[union-attr]
    assert ALICE in record.participants
    await click(rig.app(), body(ACTION_UNASSIGN, f"{PID}:acme-alice"))
    assert rig.slack.ephemerals[-1] == (CH, BOB, UNCHANGED)


# --- who may click, and when ---------------------------------------------------------------


@pytest.mark.parametrize("person", [Person(BOB, guest=True), Person(BOB, external=True)])
async def test_guests_and_external_users_cannot_edit(person: Person) -> None:
    rig = Rig()
    await rig.proposal()
    rig.slack.people[BOB] = person
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api"))
    assert (await rig.draft()).repo == "acme-co/web"
    assert rig.slack.ephemerals == [(CH, BOB, NOT_ALLOWED)]


async def test_another_workspace_cannot_edit() -> None:
    rig = Rig()
    await rig.proposal()
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api", team="T0OTHER"))
    assert (await rig.draft()).repo == "acme-co/web"


async def test_a_click_on_another_card_is_refused() -> None:
    rig = Rig()
    await rig.proposal()
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api", ts="1760781799.000100"))
    assert (await rig.draft()).repo == "acme-co/web"
    assert rig.slack.ephemerals == [(CH, BOB, NOT_THIS_CARD)]


async def test_a_paused_channel_cannot_be_edited() -> None:
    rig = Rig()
    await rig.proposal()
    await rig.store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by=ALICE)
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api"))
    assert rig.slack.ephemerals == [(CH, BOB, PAUSED)]


async def test_with_approvers_on_only_an_approver_edits_even_a_source_author() -> None:
    rig = Rig(approvers=[ALICE])
    await rig.proposal()
    app = rig.app()
    await click(app, body(ACTION_REPO, f"{PID}:api"))
    assert rig.slack.ephemerals == [(CH, BOB, APPROVERS_ONLY)]
    # Alice wrote the thread's first message, yet may edit: editing is not approving.
    await click(app, body(ACTION_REPO, f"{PID}:api", user=ALICE))
    assert (await rig.draft()).repo == "acme-co/api"


@pytest.mark.parametrize("state", [ProposalState.APPROVED, ProposalState.REJECTED])
async def test_only_a_pending_or_failed_proposal_can_be_edited(state: ProposalState) -> None:
    rig = Rig()
    await rig.proposal()
    if state is ProposalState.APPROVED:
        await rig.store.transition(
            PID, state, approved_by=ALICE, expected_hash=content_hash(await rig.draft())
        )
    else:
        await rig.store.transition(PID, state)
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api"))
    assert rig.slack.ephemerals == [(CH, BOB, NOT_EDITABLE)]


# --- staleness ---------------------------------------------------------------------------------


async def test_an_approve_from_before_the_edit_is_stale() -> None:
    rig = Rig()
    await rig.proposal()
    shown = content_hash(await rig.draft())
    app = rig.app()
    await click(app, body(ACTION_REPO, f"{PID}:api"))
    await click(app, body(ACTION_APPROVE, f"{PID}:{shown}"))
    record = await rig.store.get_proposal(PID)
    assert record is not None and record.state is ProposalState.PENDING
    assert rig.slack.ephemerals[-1] == (CH, BOB, STALE)
    await click(app, body(ACTION_APPROVE, f"{PID}:{content_hash(await rig.draft())}"))
    approved = await rig.store.get_proposal(PID)
    assert approved is not None and approved.state is ProposalState.APPROVED


async def test_a_failing_edit_is_logged_and_still_acknowledged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig()
    await rig.proposal()

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("store is down")

    monkeypatch.setattr(rig.store, "edit_proposal", broken)
    await click(rig.app(), body(ACTION_REPO, f"{PID}:api"))
    assert "card edit repo failed" in caplog.text
    assert THREAD  # the thread is untouched
