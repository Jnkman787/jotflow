"""The approval gate: Approve, Retry, Discard, and Request Changes on a card (#27, D7).

Approval guarantees, always:

- What is approved is exactly what the card showed. The Approve button
  carries the draft's content hash, and the store refuses a stale one.
- The person approving is someone JotFlow may trust. The payload's team and
  the person's own team are this workspace, and guests and external Slack
  Connect users never approve.
- The click is on the proposal's own card, in the proposal's own channel.
- With approvers on, only a listed approver may approve, and never someone
  who wrote one of the messages the proposal was drawn from, as Slack says.

JotFlow runs in Socket Mode, so Slack's request signature has no part here.
Payloads arrive over the websocket Bolt opens with the app-level token, which
only Slack can send on. The checks above are what JotFlow adds on top.

Every refusal is a short, fixed reply only the person sees, and writes nothing.
No reply ever quotes the draft or the person's text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jotflow.models import MessageRef, ProposalRecord, ProposalState, RunKind
from jotflow.orchestrator.cards import (
    ACTION_APPROVE,
    ACTION_DISCARD,
    ACTION_REQUEST_CHANGES,
    ACTION_RETRY,
)
from jotflow.orchestrator.people import Person, SlackPeople
from jotflow.policy import Policy, UnknownChannelError
from jotflow.store.base import (
    ContentChangedError,
    DraftClearedError,
    Store,
    TransitionError,
)
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import clean_question

log = logging.getLogger("jotflow.approval")

CALLBACK_CHANGES = "jotflow_changes"
CHANGES_BLOCK = "changes"
CHANGES_INPUT = "text"
MAX_CHANGES_CHARS = 2000

_PROPOSAL_ID = re.compile(r"[0-9a-f]{16}")
_APPROVE_VALUE = re.compile(r"([0-9a-f]{16}):(sha256:[0-9a-f]{64})")
_SLACK_ID = re.compile(r"[A-Z0-9]{2,24}")
_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")

# What a person sees when JotFlow says no. Fixed text: never the draft, never theirs.
NOT_ALLOWED = "Only people in this workspace who are not guests can do this."
NOT_AN_APPROVER = "Only the channel's approvers can approve proposals here."
OWN_PROPOSAL = "This proposal was drawn from your messages, so someone else needs to approve it."
AUTHORS_UNKNOWN = (
    "JotFlow could not confirm who wrote this proposal's messages, so it can't be approved "
    "while approvers are on. Try again, or request changes."
)
# Source messages checked for their authors; a proposal citing more is refused.
MAX_AUTHOR_LOOKUPS = 25
NOT_THIS_CARD = "This card no longer matches a proposal JotFlow knows about."
STALE = "This card was out of date, so nothing was approved. It now shows the current draft."
DISCARDED = "This draft was discarded, so it can no longer be approved."
CHANGES_EMPTY = "There was nothing to post once mentions and markup were removed."


def _already(state: ProposalState) -> str:
    return f"This proposal is already {state.value}, so nothing changed."


class ApprovalSlack(Protocol):
    """The Slack calls the approval gate makes. Tests use a fake."""

    async def own_team(self) -> str | None: ...

    async def person(self, user_id: str) -> Person: ...

    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None: ...

    async def open_modal(self, trigger_id: str, view: Mapping[str, Any]) -> None: ...

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None: ...

    async def authors(self, refs: Sequence[MessageRef]) -> set[str]:
        """The people who wrote these messages, from Slack. A bot's post adds no one.

        Raises if a message cannot be found, so its author is never assumed.
        """
        ...


Refresh = Callable[[str], Awaitable[object]]
ArmThread = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class ChangeRequest:
    """A checked Request Changes submission, ready to post. Holds the cleaned text."""

    proposal_id: str
    channel_id: str
    thread_ts: str
    user_id: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class Click:
    """The parts of an interaction payload the gate relies on, checked."""

    user_id: str
    channel_id: str | None
    message_ts: str | None
    trigger_id: str | None
    value: str


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class Refused(Exception):
    """The click is refused; the message is the fixed reply for the person."""


class ApprovalHandler:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        slack: ApprovalSlack,
        *,
        refresh: Refresh | None = None,
        arm_thread: ArmThread | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack
        self._refresh = refresh
        self._arm_thread = arm_thread

    # Buttons

    async def on_approve(self, body: Mapping[str, Any]) -> str:
        """Approve, or Retry a failed proposal: both lock the card's hash. Returns the outcome."""
        return await self._guarded(body, self._approve)

    async def on_discard(self, body: Mapping[str, Any]) -> str:
        """Clear a failed proposal's draft, so it can no longer be retried."""
        return await self._guarded(body, self._discard)

    async def on_request_changes(self, body: Mapping[str, Any]) -> str:
        """Open the Request Changes dialog for a pending proposal."""
        return await self._guarded(body, self._open_changes)

    async def check_changes(self, body: Mapping[str, Any]) -> ChangeRequest | str:
        """Validate a submitted dialog. Returns what to post, or the reason to show in it.

        Split from :meth:`post_changes` so the dialog is acknowledged within
        Slack's three seconds, before the slower posting.
        """
        user = _map(body.get("user"))
        user_id = _str(user.get("id")) or ""
        view = _map(body.get("view"))
        proposal_id = _str(view.get("private_metadata")) or ""
        try:
            await self._check_person(body, user_id)
            if not _PROPOSAL_ID.fullmatch(proposal_id):
                raise Refused(NOT_THIS_CARD)
            record = await self._store.get_proposal(proposal_id)
            if record is None:
                raise Refused(NOT_THIS_CARD)
            if record.state is not ProposalState.PENDING:
                raise Refused(_already(record.state))
            try:
                cleaned = clean_question(_changes_text(view)[:MAX_CHANGES_CHARS])
            except ToolError:
                raise Refused(CHANGES_EMPTY) from None
        except Refused as refused:
            log.info("change request refused for user %s", user_id or "?")
            return str(refused)
        return ChangeRequest(record.id, record.channel_id, record.thread_ts, user_id, cleaned)

    async def post_changes(self, request: ChangeRequest) -> None:
        """Post a checked change request in its thread, attributed, and arm the thread."""
        await self._slack.reply(
            request.channel_id,
            request.thread_ts,
            f"Change request from <@{request.user_id}>:\n{request.text}",
        )
        if self._arm_thread is not None:
            # JotFlow's own message never reaches the debouncer through ingest.
            await self._arm_thread(request.channel_id, request.thread_ts)
        log.info(
            "proposal %s: change request posted for user %s", request.proposal_id, request.user_id
        )

    async def on_changes_submitted(self, body: Mapping[str, Any]) -> str | None:
        """Check and post in one step. Returns None once posted, or the reason it wasn't."""
        checked = await self.check_changes(body)
        if isinstance(checked, str):
            return checked
        await self.post_changes(checked)
        return None

    # The checks, then the action

    async def _guarded(
        self,
        body: Mapping[str, Any],
        action: Callable[[Click, ProposalRecord], Awaitable[str]],
    ) -> str:
        click = _click(body)
        if click is None:
            log.warning("an approval payload was malformed; ignored")
            return "ignored"
        try:
            await self._check_person(body, click.user_id)
            record = await self._card_proposal(click)
            return await action(click, record)
        except Refused as refused:
            log.info("card action refused for user %s", click.user_id)
            await self._tell(click.user_id, str(refused), channel=click.channel_id)
            return "refused"

    async def _check_person(self, body: Mapping[str, Any], user_id: str) -> None:
        if not _SLACK_ID.fullmatch(user_id):
            raise Refused(NOT_ALLOWED)
        own = await self._slack.own_team()
        team = _map(body.get("team"))
        user = _map(body.get("user"))
        if own is None or team.get("id") != own or user.get("team_id") != own:
            raise Refused(NOT_ALLOWED)
        person = await self._slack.person(user_id)
        if person.guest or person.external:
            raise Refused(NOT_ALLOWED)

    async def _card_proposal(self, click: Click) -> ProposalRecord:
        proposal_id = click.value.split(":", 1)[0]
        record = (
            await self._store.get_proposal(proposal_id)
            if _PROPOSAL_ID.fullmatch(proposal_id)
            else None
        )
        if (
            record is None
            or click.channel_id != record.channel_id
            or click.message_ts is None
            or record.card_ts != click.message_ts
        ):
            raise Refused(NOT_THIS_CARD)
        return record

    async def _approve(self, click: Click, record: ProposalRecord) -> str:
        match = _APPROVE_VALUE.fullmatch(click.value)
        if match is None:
            raise Refused(NOT_THIS_CARD)
        approval = self._policy.approval
        if approval.approvers_enabled:
            if click.user_id not in approval.approvers:
                raise Refused(NOT_AN_APPROVER)
            if click.user_id in await self._source_authors(record):
                raise Refused(OWN_PROPOSAL)
        if record.state not in (ProposalState.PENDING, ProposalState.FAILED):
            raise Refused(_already(record.state))
        try:
            approved = await self._store.transition(
                record.id,
                ProposalState.APPROVED,
                expected={ProposalState.PENDING, ProposalState.FAILED},
                approved_by=click.user_id,
                expected_hash=match.group(2),
            )
        except ContentChangedError:
            await self._show(record)
            raise Refused(STALE) from None
        except DraftClearedError:
            raise Refused(DISCARDED) from None
        except TransitionError:
            current = await self._store.get_proposal(record.id)
            raise Refused(_already(current.state if current else record.state)) from None
        await self._store.enqueue_run(
            RunKind.EXECUTION, approved.channel_id, approved.thread_ts, proposal_id=approved.id
        )
        log.info("proposal %s approved by user %s; execution queued", approved.id, click.user_id)
        await self._show(approved)
        return "approved"

    async def _source_authors(self, record: ProposalRecord) -> set[str]:
        """Who wrote the messages the proposal was drawn from, and its thread's first one.

        From Slack itself, not from the agent's ``participants``, which name
        mentioned people too and which an agent could leave short. Fails
        closed: a message that cannot be found refuses the approval.
        """
        refs: dict[str, MessageRef] = {
            record.thread_ts: MessageRef(channel_id=record.channel_id, ts=record.thread_ts)
        }
        for ref in record.proposal.sources:
            if ref.channel_id == record.channel_id:
                refs.setdefault(ref.ts, ref)
        if len(refs) > MAX_AUTHOR_LOOKUPS:
            raise Refused(AUTHORS_UNKNOWN)
        try:
            return await self._slack.authors(list(refs.values()))
        except Exception:
            log.warning("proposal %s: could not look up its source authors", record.id)
            raise Refused(AUTHORS_UNKNOWN) from None

    async def _discard(self, click: Click, record: ProposalRecord) -> str:
        if record.state is not ProposalState.FAILED:
            raise Refused(_already(record.state))
        if self._policy.approval.approvers_enabled and click.user_id not in (
            self._policy.approval.approvers
        ):
            raise Refused(NOT_AN_APPROVER)
        cleared = await self._store.clear_draft(record.id)
        log.info("proposal %s: draft discarded by user %s", record.id, click.user_id)
        await self._show(cleared)
        return "discarded"

    async def _open_changes(self, click: Click, record: ProposalRecord) -> str:
        if record.state is not ProposalState.PENDING:
            raise Refused(_already(record.state))
        if click.trigger_id is None:
            raise Refused(NOT_THIS_CARD)
        await self._slack.open_modal(click.trigger_id, changes_modal(record.id))
        return "opened"

    async def _show(self, record: ProposalRecord) -> None:
        """Redraw the card now. The card loop (#152) would within a second anyway."""
        if self._refresh is not None:
            await self._refresh(record.id)

    async def _tell(self, user_id: str, text: str, *, channel: str | None) -> None:
        where = channel
        if not where or not _SLACK_ID.fullmatch(user_id):
            return
        try:
            self._policy.channel(where)
        except UnknownChannelError:
            return  # never reply in a channel JotFlow does not watch
        await self._slack.ephemeral(where, user_id, text)


def _click(body: Mapping[str, Any]) -> Click | None:
    user = _map(body.get("user"))
    channel = _map(body.get("channel"))
    container = _map(body.get("container"))
    actions = body.get("actions") if isinstance(body.get("actions"), list) else []
    action = _map(actions[0]) if actions else {}
    user_id = _str(user.get("id"))
    value = _str(action.get("value"))
    if user_id is None or value is None or len(value) > 128:
        return None
    message_ts = _str(container.get("message_ts"))
    return Click(
        user_id=user_id,
        channel_id=_str(channel.get("id")),
        message_ts=message_ts if message_ts and _TS.fullmatch(message_ts) else None,
        trigger_id=_str(body.get("trigger_id")),
        value=value,
    )


def _changes_text(view: Mapping[str, Any]) -> str:
    state = _map(view.get("state"))
    values = _map(state.get("values"))
    block = _map(values.get(CHANGES_BLOCK))
    field = _map(block.get(CHANGES_INPUT))
    return _str(field.get("value")) or ""


def changes_modal(proposal_id: str) -> dict[str, Any]:
    """The Request Changes dialog. Only the proposal id rides along, in private_metadata."""
    return {
        "type": "modal",
        "callback_id": CALLBACK_CHANGES,
        "private_metadata": proposal_id,
        "title": {"type": "plain_text", "text": "Request changes"},
        "submit": {"type": "plain_text", "text": "Post in thread"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": CHANGES_BLOCK,
                "label": {"type": "plain_text", "text": "What should change?"},
                "hint": {
                    "type": "plain_text",
                    "text": "Posted in the thread under your name, for the next draft.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": CHANGES_INPUT,
                    "multiline": True,
                    "max_length": MAX_CHANGES_CHARS,
                },
            }
        ],
    }


class BoltApprovalSlack:
    """:class:`ApprovalSlack` over slack-sdk's client."""

    def __init__(self, client: Any, team_id: str | None = None) -> None:
        self._client = client
        self._people = SlackPeople(client, team_id)

    async def own_team(self) -> str | None:
        return await self._people.own_team()

    async def person(self, user_id: str) -> Person:
        return await self._people.person(user_id)

    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None:
        await self._client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)

    async def open_modal(self, trigger_id: str, view: Mapping[str, Any]) -> None:
        await self._client.views_open(trigger_id=trigger_id, view=dict(view))

    async def authors(self, refs: Sequence[MessageRef]) -> set[str]:
        return await self._people.authors(refs)

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        await self._client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            link_names=False,
            unfurl_links=False,
            unfurl_media=False,
        )


def register_actions(app: Any, handler: ApprovalHandler) -> None:
    """Route the card's buttons and the Request Changes dialog to ``handler``."""

    async def run(name: str, work: Awaitable[object]) -> None:
        try:
            await work
        except Exception:
            # Never the payload: it holds the person's text and the card.
            log.exception("could not handle %s", name)

    async def approve(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("an approval", handler.on_approve(body))

    async def discard(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("a discard", handler.on_discard(body))

    async def request_changes(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("a change request", handler.on_request_changes(body))

    async def changes_submitted(ack: Any, body: dict[str, Any]) -> None:
        try:
            checked = await handler.check_changes(body)
        except Exception:
            log.exception("could not check a change request")
            checked = "JotFlow could not post this. Try again in a moment."
        if isinstance(checked, str):
            await ack(response_action="errors", errors={CHANGES_BLOCK: checked})
            return
        # Acknowledge first, within Slack's three seconds, then post.
        await ack()
        await run("a change request", handler.post_changes(checked))

    app.action(ACTION_APPROVE)(approve)
    app.action(ACTION_RETRY)(approve)
    app.action(ACTION_DISCARD)(discard)
    app.action(ACTION_REQUEST_CHANGES)(request_changes)
    app.view(CALLBACK_CHANGES)(changes_submitted)
