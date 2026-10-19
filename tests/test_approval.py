"""The approval gate: who may approve, what is locked, and what a person sees (#27, D7)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from jotflow.models import ProposalState, RunKind
from jotflow.orchestrator import approval as approval_module
from jotflow.orchestrator.approval import (
    AUTHORS_UNKNOWN,
    CALLBACK_CHANGES,
    CHANGES_BLOCK,
    CHANGES_EMPTY,
    CHANGES_INPUT,
    DISCARDED,
    NOT_ALLOWED,
    NOT_AN_APPROVER,
    NOT_THIS_CARD,
    OWN_PROPOSAL,
    STALE,
    ApprovalHandler,
    BoltApprovalSlack,
    register_actions,
)
from jotflow.orchestrator.cards import (
    ACTION_APPROVE,
    ACTION_DISCARD,
    ACTION_REQUEST_CHANGES,
    ACTION_RETRY,
    Card,
    CardContext,
    CardPublisher,
)
from jotflow.orchestrator.keyword_handler import Person
from jotflow.policy import Policy
from jotflow.store import MemoryStore, content_hash
from tests.test_runs import CH, THREAD, _proposal_record

TEAM = "T0OURS"
CARD_TS = "1760781700.000900"
ALICE, BOB, CAROL = "U0ALICE", "U0BOB", "U0CAROL"


def policy(approvers: list[str] | None = None) -> Policy:
    data: dict[str, Any] = {
        "channels": {
            CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]}
        }
    }
    if approvers is not None:
        data["approval"] = {"approvers": approvers}
    return Policy.model_validate(data)


class Slack:
    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self.ephemerals: list[tuple[str, str, str]] = []
        self.modals: list[tuple[str, Mapping[str, Any]]] = []
        self.replies: list[tuple[str, str, str]] = []
        # Who wrote each message, by ts, as Slack would say: None for a bot's post,
        # and a missing ts means the message was not found.
        self.written_by: dict[str, str | None] = {THREAD: ALICE}
        self.author_lookups: list[list[str]] = []

    async def authors(self, refs: Any) -> set[str]:
        self.author_lookups.append([r.ts for r in refs])
        found = set()
        for ref in refs:
            if ref.ts not in self.written_by:
                raise LookupError(ref.ts)
            author = self.written_by[ref.ts]
            if author is not None:
                found.add(author)
        return found

    async def own_team(self) -> str | None:
        return TEAM

    async def person(self, user_id: str) -> Person:
        return self.people.get(user_id, Person(user_id))

    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None:
        self.ephemerals.append((channel_id, user_id, text))

    async def open_modal(self, trigger_id: str, view: Mapping[str, Any]) -> None:
        self.modals.append((trigger_id, view))

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        self.replies.append((channel_id, thread_ts, text))


class Cards:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str, Card]] = []

    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        raise AssertionError("the gate only edits the existing card")

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        self.updates.append((channel_id, ts, card))


class Rig:
    def __init__(self, approvers: list[str] | None = None) -> None:
        self.store = MemoryStore()
        self.slack = Slack()
        self.cards = Cards()
        self.armed: list[tuple[str, str]] = []
        self._policy = policy(approvers)
        self._publisher = CardPublisher(self.store, self.cards)
        self.handler = ApprovalHandler(
            self.store, self._policy, self.slack, refresh=self._refresh, arm_thread=self._arm
        )

    async def _refresh(self, proposal_id: str) -> None:
        # What CardSync.refresh does in the app (#152), with a minimal context.
        record = await self.store.get_proposal(proposal_id)
        assert record is not None
        await self._publisher.show(record, CardContext(channel=self._policy.channel(CH)))

    async def _arm(self, channel_id: str, thread_ts: str) -> None:
        self.armed.append((channel_id, thread_ts))

    async def proposal(
        self, *, participants: list[str] | None = None, sources: list[Any] | None = None
    ) -> str:
        record = _proposal_record("0123456789abcdef")
        record = record.model_copy(
            update={
                "participants": participants or [ALICE],
                "proposal": record.proposal.model_copy(update={"sources": sources or []}),
            }
        )
        await self.store.add_proposal(record)
        await self.store.set_card_ts(record.id, CARD_TS)
        return record.id

    async def hash(self, proposal_id: str) -> str:
        record = await self.store.get_proposal(proposal_id)
        assert record is not None
        return content_hash(record.proposal)

    async def state(self, proposal_id: str) -> ProposalState:
        record = await self.store.get_proposal(proposal_id)
        assert record is not None
        return record.state

    async def executions(self) -> int:
        runs = await self.store.thread_runs(CH, THREAD)
        return sum(1 for r in runs if r.kind is RunKind.EXECUTION)


def click(
    value: str,
    *,
    user: str = BOB,
    user_team: str = TEAM,
    team: str = TEAM,
    channel: str = CH,
    ts: str = CARD_TS,
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": user, "team_id": user_team},
        "team": {"id": team},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": ts, "channel_id": channel},
        "trigger_id": "1.2.trigger",
        "actions": [{"action_id": ACTION_APPROVE, "value": value}],
    }


def submission(proposal_id: str, text: str, *, user: str = BOB, team: str = TEAM) -> dict:
    return {
        "type": "view_submission",
        "user": {"id": user, "team_id": team},
        "team": {"id": team},
        "view": {
            "callback_id": CALLBACK_CHANGES,
            "private_metadata": proposal_id,
            "state": {"values": {CHANGES_BLOCK: {CHANGES_INPUT: {"value": text}}}},
        },
    }


# Approve


async def test_an_approval_locks_the_hash_records_the_approver_and_queues_the_run() -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_approve(click(f"{pid}:{await rig.hash(pid)}")) == "approved"
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.state is ProposalState.APPROVED
    assert record.approved_by == BOB and record.approved_at is not None
    assert record.content_hash == content_hash(record.proposal)
    assert await rig.executions() == 1
    ((channel, ts, card),) = rig.cards.updates
    assert (channel, ts) == (CH, CARD_TS) and BOB in str(card.blocks)
    assert rig.slack.ephemerals == []


async def test_a_stale_card_approves_nothing_and_is_refreshed() -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_approve(click(f"{pid}:sha256:{'0' * 64}")) == "refused"
    assert await rig.state(pid) is ProposalState.PENDING and await rig.executions() == 0
    assert rig.slack.ephemerals == [(CH, BOB, STALE)]
    assert len(rig.cards.updates) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"team": "T0OTHER"},
        {"user_team": "T0OTHER"},
        {"user": "not-an-id"},
    ],
)
async def test_someone_from_another_workspace_cannot_approve(change: dict[str, str]) -> None:
    rig = Rig()
    pid = await rig.proposal()
    body = click(f"{pid}:{await rig.hash(pid)}", **change)
    assert await rig.handler.on_approve(body) == "refused"
    assert await rig.state(pid) is ProposalState.PENDING and await rig.executions() == 0


@pytest.mark.parametrize("person", [Person(BOB, guest=True), Person(BOB, external=True)])
async def test_guests_and_external_users_never_approve(person: Person) -> None:
    rig = Rig()
    rig.slack.people[BOB] = person
    pid = await rig.proposal()
    assert await rig.handler.on_approve(click(f"{pid}:{await rig.hash(pid)}")) == "refused"
    assert rig.slack.ephemerals == [(CH, BOB, NOT_ALLOWED)]
    assert await rig.state(pid) is ProposalState.PENDING


@pytest.mark.parametrize(
    "change",
    [{"channel": "C0OTHER01"}, {"ts": "1760781799.000100"}],
)
async def test_the_click_must_be_on_the_proposals_own_card(change: dict[str, str]) -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_approve(click(f"{pid}:{await rig.hash(pid)}", **change)) == (
        "refused"
    )
    assert await rig.state(pid) is ProposalState.PENDING
    if "channel" in change:
        # Never a reply in a channel JotFlow does not watch.
        assert rig.slack.ephemerals == []
    else:
        assert rig.slack.ephemerals == [(CH, BOB, NOT_THIS_CARD)]


@pytest.mark.parametrize("value", ["", "x" * 200, "nothex:abc", "0123456789abcdef"])
async def test_a_malformed_value_changes_nothing(value: str) -> None:
    rig = Rig()
    pid = await rig.proposal()
    outcome = await rig.handler.on_approve(click(value))
    assert outcome in ("ignored", "refused")
    assert await rig.state(pid) is ProposalState.PENDING


async def test_a_malformed_payload_is_ignored() -> None:
    rig = Rig()
    assert await rig.handler.on_approve({"type": "block_actions"}) == "ignored"
    assert await rig.handler.on_approve({"user": "U0BOB", "actions": "x"}) == "ignored"


async def test_with_approvers_on_only_a_listed_approver_who_wrote_none_of_it_approves() -> None:
    rig = Rig(approvers=[ALICE, CAROL])
    pid = await rig.proposal(participants=[ALICE, CAROL])  # Carol was only mentioned
    good = f"{pid}:{await rig.hash(pid)}"
    assert await rig.handler.on_approve(click(good, user=BOB)) == "refused"
    assert await rig.handler.on_approve(click(good, user=ALICE)) == "refused"
    assert [e[2] for e in rig.slack.ephemerals] == [NOT_AN_APPROVER, OWN_PROPOSAL]
    assert await rig.handler.on_approve(click(good, user=CAROL)) == "approved"
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.approved_by == CAROL


async def test_the_author_of_a_cited_reply_cannot_approve_either() -> None:
    from jotflow.models import MessageRef

    reply = "1760781650.000300"
    rig = Rig(approvers=[CAROL])
    pid = await rig.proposal(sources=[MessageRef(channel_id=CH, ts=reply, thread_ts=THREAD)])
    rig.slack.written_by[reply] = CAROL
    good = f"{pid}:{await rig.hash(pid)}"
    assert await rig.handler.on_approve(click(good, user=CAROL)) == "refused"
    assert rig.slack.ephemerals == [(CH, CAROL, OWN_PROPOSAL)]
    assert sorted(rig.slack.author_lookups[0]) == sorted([THREAD, reply])


async def test_authors_that_cannot_be_confirmed_refuse_the_approval() -> None:
    from jotflow.models import MessageRef

    rig = Rig(approvers=[CAROL])
    pid = await rig.proposal(sources=[MessageRef(channel_id=CH, ts="1760781650.000300")])
    good = f"{pid}:{await rig.hash(pid)}"
    assert await rig.handler.on_approve(click(good, user=CAROL)) == "refused"
    assert rig.slack.ephemerals == [(CH, CAROL, AUTHORS_UNKNOWN)]
    many = Rig(approvers=[CAROL])
    refs = [MessageRef(channel_id=CH, ts=f"1760781650.{i:06d}") for i in range(30)]
    pid = await many.proposal(sources=refs)
    good = f"{pid}:{await many.hash(pid)}"
    assert await many.handler.on_approve(click(good, user=CAROL)) == "refused"
    assert many.slack.author_lookups == []


async def test_with_approvers_off_the_poster_may_approve() -> None:
    rig = Rig()
    rig.slack.written_by[THREAD] = BOB
    pid = await rig.proposal(participants=[BOB])
    assert await rig.handler.on_approve(click(f"{pid}:{await rig.hash(pid)}")) == "approved"
    assert rig.slack.author_lookups == []  # no lookup unless approvers are on


async def test_a_second_approval_changes_nothing() -> None:
    rig = Rig()
    pid = await rig.proposal()
    good = f"{pid}:{await rig.hash(pid)}"
    await rig.handler.on_approve(click(good))
    assert await rig.handler.on_approve(click(good, user=CAROL)) == "refused"
    assert rig.slack.ephemerals[-1][2] == "This proposal is already approved, so nothing changed."
    assert await rig.executions() == 1


async def test_a_lost_race_names_the_current_state(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig()
    pid = await rig.proposal()
    good = f"{pid}:{await rig.hash(pid)}"
    real = rig.store.get_proposal
    calls = 0

    async def stale_first(proposal_id: str) -> Any:
        nonlocal calls
        calls += 1
        record = await real(proposal_id)
        if calls == 1:
            # Another click approves between this click's read and its write.
            await rig.store.transition(
                pid, ProposalState.APPROVED, approved_by=CAROL, expected_hash=await rig.hash(pid)
            )
        return record

    monkeypatch.setattr(rig.store, "get_proposal", stale_first)
    assert await rig.handler.on_approve(click(good)) == "refused"
    assert rig.slack.ephemerals[-1][2] == "This proposal is already approved, so nothing changed."


# Retry and Discard on a failed card


async def _failed(rig: Rig, pid: str) -> None:
    await rig.store.transition(
        pid, ProposalState.APPROVED, approved_by=CAROL, expected_hash=await rig.hash(pid)
    )
    await rig.store.transition(pid, ProposalState.EXECUTING)
    await rig.store.transition(pid, ProposalState.FAILED)


async def test_retry_approves_a_failed_proposal_again_with_its_hash() -> None:
    rig = Rig()
    pid = await rig.proposal()
    await _failed(rig, pid)
    body = click(f"{pid}:{await rig.hash(pid)}")
    body["actions"][0]["action_id"] = ACTION_RETRY
    assert await rig.handler.on_approve(body) == "approved"
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.approved_by == BOB
    assert await rig.executions() == 1


async def test_discard_clears_a_failed_draft_and_retry_is_then_refused() -> None:
    rig = Rig()
    pid = await rig.proposal()
    good = f"{pid}:{await rig.hash(pid)}"
    await _failed(rig, pid)
    assert await rig.handler.on_discard(click(pid)) == "discarded"
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.draft_cleared_at is not None
    assert "jotflow_retry" not in str(rig.cards.updates[-1][2].blocks)
    assert await rig.handler.on_approve(click(good)) == "refused"
    assert rig.slack.ephemerals[-1][2] in (DISCARDED, STALE)


async def test_discard_only_on_a_failed_proposal_and_by_an_approver_when_on() -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_discard(click(pid)) == "refused"
    strict = Rig(approvers=[CAROL])
    pid = await strict.proposal()
    await _failed(strict, pid)
    assert await strict.handler.on_discard(click(pid, user=BOB)) == "refused"
    assert strict.slack.ephemerals == [(CH, BOB, NOT_AN_APPROVER)]
    assert await strict.handler.on_discard(click(pid, user=CAROL)) == "discarded"


# Request Changes


async def test_request_changes_opens_a_dialog_carrying_only_the_proposal_id() -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_request_changes(click(pid)) == "opened"
    ((trigger, view),) = rig.slack.modals
    assert trigger == "1.2.trigger"
    assert view["callback_id"] == CALLBACK_CHANGES and view["private_metadata"] == pid
    element = view["blocks"][0]["element"]
    assert element["type"] == "plain_text_input" and element["max_length"] == 2000


async def test_request_changes_needs_a_pending_proposal_and_a_trigger() -> None:
    rig = Rig()
    pid = await rig.proposal()
    body = click(pid)
    del body["trigger_id"]
    assert await rig.handler.on_request_changes(body) == "refused"
    await rig.handler.on_approve(click(f"{pid}:{await rig.hash(pid)}"))
    assert await rig.handler.on_request_changes(click(pid)) == "refused"
    assert rig.slack.modals == []


async def test_a_change_request_is_posted_attributed_and_arms_the_thread() -> None:
    rig = Rig()
    pid = await rig.proposal()
    text = "Use <!channel> the *blue* <https://evil.example|link> please"
    assert await rig.handler.on_changes_submitted(submission(pid, text)) is None
    ((channel, thread, posted),) = rig.slack.replies
    assert (channel, thread) == (CH, THREAD)
    assert posted.startswith(f"Change request from <@{BOB}>:\n")
    assert "<!channel>" not in posted and "<https://" not in posted
    assert "&lt;https://evil.example|link&gt;" in posted
    assert rig.armed == [(CH, THREAD)]


async def test_a_change_request_that_is_empty_once_cleaned_is_refused() -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_changes_submitted(submission(pid, "  <!here>  ")) == CHANGES_EMPTY
    assert rig.slack.replies == [] and rig.armed == []


@pytest.mark.parametrize("person", [Person(BOB, guest=True), Person(BOB, external=True)])
async def test_a_guest_or_external_change_request_is_refused(person: Person) -> None:
    rig = Rig()
    rig.slack.people[BOB] = person
    pid = await rig.proposal()
    assert await rig.handler.on_changes_submitted(submission(pid, "x")) == NOT_ALLOWED
    assert rig.slack.replies == []


async def test_a_change_request_for_a_gone_or_settled_proposal_is_refused() -> None:
    rig = Rig()
    pid = await rig.proposal()
    assert await rig.handler.on_changes_submitted(submission("../etc", "x")) == NOT_THIS_CARD
    assert await rig.handler.on_changes_submitted(submission("f" * 16, "x")) == NOT_THIS_CARD
    await rig.handler.on_approve(click(f"{pid}:{await rig.hash(pid)}"))
    refused = await rig.handler.on_changes_submitted(submission(pid, "x"))
    assert refused == "This proposal is already approved, so nothing changed."
    assert await rig.handler.on_changes_submitted(submission(pid, "x", team="T0OTHER")) == (
        NOT_ALLOWED
    )


# Bolt wiring


class App:
    def __init__(self) -> None:
        self.actions: dict[str, Any] = {}
        self.views: dict[str, Any] = {}

    def action(self, name: str) -> Any:
        def register(fn: Any) -> Any:
            self.actions[name] = fn
            return fn

        return register

    def view(self, name: str) -> Any:
        def register(fn: Any) -> Any:
            self.views[name] = fn
            return fn

        return register


class Ack:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def test_the_actions_ack_first_and_errors_go_back_to_the_dialog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    pid = await rig.proposal()
    app = App()
    register_actions(app, rig.handler)
    assert set(app.actions) == {
        ACTION_APPROVE,
        ACTION_RETRY,
        ACTION_DISCARD,
        ACTION_REQUEST_CHANGES,
    }
    ack = Ack()
    await app.actions[ACTION_APPROVE](ack, click(f"{pid}:{await rig.hash(pid)}"))
    assert ack.calls == [{}] and await rig.state(pid) is ProposalState.APPROVED
    ack = Ack()
    await app.actions[ACTION_DISCARD](ack, click(pid))
    await app.actions[ACTION_REQUEST_CHANGES](ack, click(pid))
    assert ack.calls == [{}, {}]
    ack = Ack()
    await app.views[CALLBACK_CHANGES](ack, submission(pid, "x"))
    assert ack.calls == [
        {
            "response_action": "errors",
            "errors": {CHANGES_BLOCK: "This proposal is already approved, so nothing changed."},
        }
    ]


async def test_a_handler_error_is_logged_without_the_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig()
    app = App()
    register_actions(app, rig.handler)

    async def broken(*_: Any) -> Any:
        raise RuntimeError("slack is down")

    monkeypatch.setattr(rig.handler, "on_approve", broken)
    monkeypatch.setattr(rig.handler, "check_changes", broken)
    ack = Ack()
    await app.actions[ACTION_APPROVE](ack, click("SECRET-VALUE"))
    await app.views[CALLBACK_CHANGES](ack, submission("x", "SECRET-TEXT"))
    assert "could not handle an approval" in caplog.text
    assert "SECRET" not in caplog.text
    assert ack.calls[-1]["response_action"] == "errors"


async def test_a_pending_card_without_a_card_ts_is_not_this_card() -> None:
    rig = Rig()
    record = _proposal_record("fedcba9876543210")
    await rig.store.add_proposal(record)
    assert await rig.handler.on_approve(click(f"{record.id}:sha256:{'a' * 64}")) == "refused"
    assert rig.slack.ephemerals == [(CH, BOB, NOT_THIS_CARD)]


# The Bolt adapter


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def auth_test(self) -> dict[str, Any]:
        self.calls.append(("auth_test", {}))
        return {"team_id": TEAM}

    async def users_info(self, user: str) -> dict[str, Any]:
        return {"user": {"id": user, "team_id": TEAM, "is_restricted": user == "U0GUEST"}}

    async def chat_postEphemeral(self, **kwargs: Any) -> None:
        self.calls.append(("chat_postEphemeral", kwargs))

    async def views_open(self, **kwargs: Any) -> None:
        self.calls.append(("views_open", kwargs))

    async def chat_postMessage(self, **kwargs: Any) -> None:
        self.calls.append(("chat_postMessage", kwargs))

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("conversations_replies", kwargs))
        reply = {"ts": "1760781650.000300", "user": BOB}
        messages = {
            THREAD: [{"ts": THREAD, "user": ALICE}],
            "1760781650.000300": [{"ts": THREAD, "user": ALICE}, reply],
            "1760781651.000300": [{"ts": THREAD, "user": ALICE}],
            "1760781652.000300": [{"ts": "1760781652.000300", "bot_id": "B0ALERTS"}],
            "1760781653.000300": [{"ts": "1760781653.000300", "subtype": "bot_message"}],
            "1760781654.000300": [{"ts": "1760781654.000300", "text": "no author at all"}],
        }
        return {"messages": messages.get(kwargs["oldest"], [])}


async def test_the_bolt_adapter_posts_without_links_or_mentions_expanding() -> None:
    client = Client()
    slack = BoltApprovalSlack(client)
    assert await slack.own_team() == TEAM and await slack.own_team() == TEAM
    assert [c[0] for c in client.calls].count("auth_test") == 1
    assert (await slack.person("U0GUEST")).guest and not (await slack.person(BOB)).guest
    await slack.ephemeral(CH, BOB, "no")
    await slack.open_modal("t", {"type": "modal"})
    await slack.reply(CH, THREAD, "text")
    posted = dict(client.calls)["chat_postMessage"]
    assert posted["link_names"] is False and posted["unfurl_links"] is False
    assert posted["unfurl_media"] is False and posted["thread_ts"] == THREAD


def test_the_module_says_how_socket_mode_replaces_the_signature() -> None:
    assert "Socket Mode" in (approval_module.__doc__ or "")


async def test_a_valid_dialog_is_acknowledged_before_the_reply_is_posted() -> None:
    rig = Rig()
    pid = await rig.proposal()
    app = App()
    register_actions(app, rig.handler)
    order: list[str] = []

    class Ordered(Ack):
        async def __call__(self, **kwargs: Any) -> None:
            order.append("ack")
            await super().__call__(**kwargs)

    real = rig.slack.reply

    async def reply(channel_id: str, thread_ts: str, text: str) -> None:
        order.append("reply")
        await real(channel_id, thread_ts, text)

    rig.slack.reply = reply  # type: ignore[method-assign]
    await app.views[CALLBACK_CHANGES](Ordered(), submission(pid, "make it blue"))
    assert order == ["ack", "reply"]
    assert rig.armed == [(CH, THREAD)]
    assert "make it blue" not in repr(
        await rig.handler.check_changes(submission(pid, "make it blue"))
    )


async def test_the_bolt_adapter_reads_authors_from_slack_and_fails_closed() -> None:
    from jotflow.models import MessageRef

    client = Client()
    slack = BoltApprovalSlack(client, TEAM)
    reply = MessageRef(channel_id=CH, ts="1760781650.000300", thread_ts=THREAD)
    assert await slack.authors([MessageRef(channel_id=CH, ts=THREAD), reply]) == {ALICE, BOB}
    lookup = dict(client.calls)["conversations_replies"]
    assert lookup["ts"] == THREAD and lookup["inclusive"] is True and lookup["limit"] == 1
    gone = MessageRef(channel_id=CH, ts="1760781651.000300", thread_ts=THREAD)
    with pytest.raises(LookupError):
        await slack.authors([gone])


# Through the real Bolt app: the payload Slack sends, routed by register_actions


async def _authorize(**_: Any) -> Any:
    from slack_bolt.authorization import AuthorizeResult

    return AuthorizeResult(
        enterprise_id=None, team_id=TEAM, bot_user_id="U0BOT", bot_id="B0BOT", bot_token="xoxb-t"
    )


def _real_app(rig: Rig) -> Any:
    from slack_bolt.async_app import AsyncApp
    from slack_sdk.web.async_client import AsyncWebClient

    app = AsyncApp(
        client=AsyncWebClient(token="xoxb-t"),
        authorize=_authorize,
        request_verification_enabled=False,
        process_before_response=True,
    )
    register_actions(app, rig.handler)
    return app


async def _press(app: Any, body: dict[str, Any], action_id: str = ACTION_APPROVE) -> None:
    import json
    from urllib.parse import urlencode

    from slack_bolt.request.async_request import AsyncBoltRequest

    payload = dict(body)
    payload["api_app_id"] = "A0JOTFLOW"
    payload["actions"] = [
        {**payload["actions"][0], "action_id": action_id, "block_id": "b", "type": "button"}
    ]
    request = AsyncBoltRequest(
        body=urlencode({"payload": json.dumps(payload)}),
        headers={"content-type": ["application/x-www-form-urlencoded"]},
    )
    response = await app.async_dispatch(request)
    assert response.status == 200


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_real_app_a_stale_hash_approves_nothing_and_refreshes_the_card() -> None:
    rig = Rig()
    pid = await rig.proposal()
    await _press(_real_app(rig), click(f"{pid}:sha256:{'0' * 64}"))
    assert await rig.state(pid) is ProposalState.PENDING and await rig.executions() == 0
    assert rig.slack.ephemerals == [(CH, BOB, STALE)] and len(rig.cards.updates) == 1


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_real_app_approvers_on_refuse_a_non_approver_and_a_source_author() -> None:
    from jotflow.models import MessageRef

    rig = Rig(approvers=[ALICE, CAROL])
    reply = "1760781650.000300"
    rig.slack.written_by[reply] = CAROL
    pid = await rig.proposal(
        participants=[ALICE, CAROL, BOB],
        sources=[MessageRef(channel_id=CH, ts=reply, thread_ts=THREAD)],
    )
    app = _real_app(rig)
    good = f"{pid}:{await rig.hash(pid)}"
    await _press(app, click(good, user=BOB))  # not listed
    await _press(app, click(good, user=CAROL))  # wrote a cited source
    await _press(app, click(good, user=ALICE))  # wrote the first message
    assert [e[2] for e in rig.slack.ephemerals] == [NOT_AN_APPROVER, OWN_PROPOSAL, OWN_PROPOSAL]
    assert await rig.state(pid) is ProposalState.PENDING and await rig.executions() == 0


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_real_app_a_mentioned_approver_may_approve() -> None:
    rig = Rig(approvers=[CAROL])
    pid = await rig.proposal(participants=[ALICE, CAROL])  # Carol only mentioned
    await _press(_real_app(rig), click(f"{pid}:{await rig.hash(pid)}", user=CAROL))
    record = await rig.store.get_proposal(pid)
    assert record is not None and record.state is ProposalState.APPROVED
    assert record.approved_by == CAROL and await rig.executions() == 1


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_real_app_a_guest_and_another_workspace_are_refused() -> None:
    rig = Rig()
    rig.slack.people[BOB] = Person(BOB, guest=True)
    pid = await rig.proposal()
    app = _real_app(rig)
    good = f"{pid}:{await rig.hash(pid)}"
    await _press(app, click(good, user=BOB))
    await _press(app, click(good, user=CAROL, user_team="T0OTHER"))
    assert [e[2] for e in rig.slack.ephemerals] == [NOT_ALLOWED, NOT_ALLOWED]
    assert await rig.state(pid) is ProposalState.PENDING and await rig.executions() == 0


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_real_app_retry_and_discard_route_to_the_gate() -> None:
    rig = Rig()
    pid = await rig.proposal()
    await _failed(rig, pid)
    app = _real_app(rig)
    await _press(app, click(f"{pid}:{await rig.hash(pid)}"), ACTION_RETRY)
    assert await rig.state(pid) is ProposalState.APPROVED


async def test_the_bolt_adapter_skips_a_bots_post_but_not_a_missing_author() -> None:
    from jotflow.models import MessageRef

    slack = BoltApprovalSlack(Client(), TEAM)
    bots = [MessageRef(channel_id=CH, ts=t) for t in ("1760781652.000300", "1760781653.000300")]
    assert await slack.authors(bots) == set()
    with pytest.raises(LookupError):
        await slack.authors([MessageRef(channel_id=CH, ts="1760781654.000300")])


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_real_app_a_thread_rooted_in_a_bot_post_can_be_approved() -> None:
    from jotflow.models import MessageRef

    rig = Rig(approvers=[CAROL])
    rig.slack.written_by[THREAD] = None  # an alert bot started the thread
    pid = await rig.proposal()
    await _press(_real_app(rig), click(f"{pid}:{await rig.hash(pid)}", user=CAROL))
    assert await rig.state(pid) is ProposalState.APPROVED
    # A cited message that has since been deleted still refuses.
    deleted = Rig(approvers=[CAROL])
    deleted.slack.written_by[THREAD] = None
    pid = await deleted.proposal(sources=[MessageRef(channel_id=CH, ts="1760781699.000100")])
    await _press(_real_app(deleted), click(f"{pid}:{await deleted.hash(pid)}", user=CAROL))
    assert deleted.slack.ephemerals == [(CH, CAROL, AUTHORS_UNKNOWN)]
    assert await deleted.state(pid) is ProposalState.PENDING
