"""The approval gate: Approve, Retry, Discard, and Request Changes on a card (#27, D7).

Approval guarantees, always:

- What is approved is exactly what the card showed. The Approve button
  carries the draft's content hash, and the store refuses a stale one.
- The person approving is someone JotFlow may trust. The payload's team and
  the person's own team are this workspace, and guests and external Slack
  Connect users never approve.
- The click is on the proposal's own card, in the proposal's own channel.
- With approvers on, only a listed approver may approve. With
  ``require_second_approver`` too, never someone who wrote one of the
  messages the proposal was drawn from, as Slack says (#244).

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

from jotflow.models import ChannelMode, MessageRef, ProposalRecord, ProposalState, RunKind
from jotflow.orchestrator.cards import (
    ACTION_APPROVE,
    ACTION_DISCARD,
    ACTION_NOT_DUPLICATE,
    ACTION_REQUEST_CHANGES,
    ACTION_RETRY,
    ACTION_SAME_AS,
    CardContext,
    approvable,
)
from jotflow.orchestrator.people import Person, SlackPeople
from jotflow.policy import Policy, PolicyViolation, UnknownChannelError
from jotflow.repo_catalog import RepoCatalog
from jotflow.store import content_hash
from jotflow.store.base import MAX_AUTHOR_LOOKUPS as STORE_AUTHOR_LOOKUPS
from jotflow.store.base import (
    ContentChangedError,
    DraftClearedError,
    DuplicateRefusedError,
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
# "Same as" (#36): the proposal, the hash of the draft the card showed, and which
# related issue. The issue itself is read from the stored draft, never from here.
_SAME_AS_VALUE = re.compile(r"([0-9a-f]{16}):(sha256:[0-9a-f]{64}):([0-9])")
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
# One limit, shared with revisions, so a revised card stays approvable (#304).
MAX_AUTHOR_LOOKUPS = STORE_AUTHOR_LOOKUPS
NOT_THIS_CARD = "This card no longer matches a proposal JotFlow knows about."
# Once a card has changed in place, only its button approves: the button carries the
# hash of the draft it shows, and a reaction can't (#304).
REDRAWN_USE_BUTTON = (
    "This card changed since it was posted, so a reaction can't approve it. "
    "Reread it, then use its Approve button."
)
STALE = "This card was out of date, so nothing was approved. It now shows the current draft."
DISCARDED = "This draft was discarded, so it can no longer be approved."
STALE_SAME_AS = "This card was out of date, so nothing was marked. It now shows the current draft."
CHANGES_EMPTY = "There was nothing to post once mentions and markup were removed."
CARD_UPDATING = "This card is being updated, so nothing was approved. React again in a moment."
TOO_LONG_TO_REACT = (
    "This draft is too long to show in full on the card, so it can't be approved here."
)
# A repo a channel's scope found (#236): checked again at approval, failing closed.
REPO_GONE = "This repo can't be used here any more, so nothing was approved: {reason}"
NOT_WRITABLE = (
    "JotFlow's write token can't reach {repo}. Give the token access to that repo on "
    "GitHub, then approve again."
)
WRITE_UNCHECKED = (
    "JotFlow couldn't check write access to {repo}, so nothing was approved. Try again."
)
PAUSED = "JotFlow is paused in this channel. Nothing is written until someone sets it back."


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
ContextFor = Callable[[ProposalRecord], Awaitable[CardContext]]
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
        context_for: ContextFor | None = None,
        repos: RepoCatalog | None = None,
    ) -> None:
        self._context_for = context_for
        self._store = store
        self._policy = policy
        self._repos = repos if repos is not None else RepoCatalog.static(policy)
        self._slack = slack
        self._refresh = refresh
        self._arm_thread = arm_thread

    # Buttons

    async def on_approve(self, body: Mapping[str, Any]) -> str:
        """Approve, or Retry a failed proposal: both lock the card's hash. Returns the outcome."""
        return await self._guarded(body, self._approve)

    async def on_same_as(self, body: Mapping[str, Any]) -> str:
        """Mark a pending proposal the same as its best related issue (#36)."""
        return await self._guarded(body, self._same_as)

    async def on_not_duplicate(self, body: Mapping[str, Any]) -> str:
        """Take back a "same as", and draft the thread again (#36)."""
        return await self._guarded(body, self._not_duplicate)

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
            await self._check_not_paused(record.channel_id)
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

    # A reaction on the card (#34)

    async def on_reaction(self, event: Mapping[str, Any]) -> str:
        """The configured reaction on a pending card counts as Approve, when turned on.

        A reaction carries no hash, so it approves only a card that shows the
        stored draft: drawn at the proposal's current revision (#152), and in
        full, as the Approve button requires. Every other check is the
        button's. A reaction that is off, another emoji, or not on a card is
        ignored without a word.
        """
        approval = self._policy.approval
        # Slack names a toned emoji "thumbsup::skin-tone-2"; the tone is the same reaction.
        name = str(event.get("reaction") or "").split("::", 1)[0]
        if not approval.reactions or name != approval.reaction:
            return "ignored"
        item = _map(event.get("item"))
        user_id = _str(event.get("user"))
        channel = _str(item.get("channel"))
        ts = _str(item.get("ts"))
        if item.get("type") != "message" or not user_id or not channel or not ts:
            return "ignored"
        record = await self._store.proposal_for_card(channel, ts)
        if record is None:
            return "ignored"
        click = Click(
            user_id=user_id,
            channel_id=channel,
            message_ts=ts,
            trigger_id=None,
            value=f"{record.id}:{content_hash(record.proposal)}",
        )
        try:
            # A reaction event has no payload teams; users.info decides external.
            await self._check_person(event, user_id, payload_teams=False)
            await self._check_not_paused(record.channel_id)
            if record.state is not ProposalState.PENDING:
                raise Refused(_already(record.state))
            if record.card_due:
                raise Refused(CARD_UPDATING)
            if await self._redrawn(record):
                raise Refused(REDRAWN_USE_BUTTON)
            context = (
                await self._context_for(record)
                if self._context_for is not None
                else CardContext(
                    channel=self._policy.channel(record.channel_id), issues=self._policy.issues
                )
            )
            if not approvable(record, context):
                raise Refused(TOO_LONG_TO_REACT)
            return await self._approve(click, record)
        except Refused as refused:
            log.info("reaction approval refused for user %s", user_id)
            await self._tell(user_id, str(refused), channel=channel)
            return "refused"

    # The checks, then the action

    async def guarded(
        self,
        body: Mapping[str, Any],
        action: Callable[[Click, ProposalRecord], Awaitable[str]],
    ) -> str:
        """Run ``action`` for a card click, once the click passes the gate's checks.

        The checks are those every card action shares: this workspace's team
        for the payload and the person, no guests or external users, the
        proposal's own card in its own channel, and a channel that isn't
        paused. Who may *approve* (the approver list and authorship) is
        Approve's own check, inside its action. ``action`` may raise
        :class:`Refused`; its fixed text reaches only the person. Returns the
        action's outcome, ``"refused"``, or ``"ignored"`` for a malformed payload.
        Card edits (#160) use this entry point.
        """
        return await self._guarded(body, action)

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
            await self._check_not_paused(record.channel_id)
            return await action(click, record)
        except Refused as refused:
            log.info("card action refused for user %s", click.user_id)
            await self._tell(click.user_id, str(refused), channel=click.channel_id)
            return "refused"

    async def _check_not_paused(self, channel_id: str) -> None:
        """A paused channel reads nothing and writes nothing, its cards included (#28)."""
        setting = await self._store.get_channel_mode(channel_id)
        if setting is not None and setting.mode is ChannelMode.PAUSED:
            raise Refused(PAUSED)

    async def _check_person(
        self, body: Mapping[str, Any], user_id: str, *, payload_teams: bool = True
    ) -> None:
        if not _SLACK_ID.fullmatch(user_id):
            raise Refused(NOT_ALLOWED)
        own = await self._slack.own_team()
        if own is None:
            raise Refused(NOT_ALLOWED)
        if payload_teams:
            team = _map(body.get("team"))
            user = _map(body.get("user"))
            if team.get("id") != own or user.get("team_id") != own:
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

    async def _redrawn(self, record: ProposalRecord) -> bool:
        """Whether the card was redrawn in place since it was first posted (#304).

        A reaction names only the card's message, not what it showed, so one made
        while someone read an earlier draft could approve a later one. A pending
        card is redrawn in place only by a revision from new messages or a person's
        edit from the card, and the store records both.
        """
        if await self._store.proposal_revisions(record.id):
            return True
        return bool(await self._store.proposal_edits(record.id))

    async def _check_may_approve(self, click: Click, record: ProposalRecord) -> None:
        """Who may approve: Approve's rule, which "same as" and its undo share (#36)."""
        approval = self._policy.approval
        if approval.approvers_enabled:
            if click.user_id not in approval.approvers:
                raise Refused(NOT_AN_APPROVER)
            # A listed approver may approve their own proposal unless the team
            # asks for a second person (#244). Only this clause is optional.
            if approval.require_second_approver and click.user_id in await self._source_authors(
                record
            ):
                raise Refused(OWN_PROPOSAL)

    async def _approve(self, click: Click, record: ProposalRecord) -> str:
        match = _APPROVE_VALUE.fullmatch(click.value)
        if match is None:
            raise Refused(NOT_THIS_CARD)
        await self._check_may_approve(click, record)
        if record.state not in (ProposalState.PENDING, ProposalState.FAILED):
            raise Refused(_already(record.state))
        await self._check_repo(record)
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

    async def _check_repo(self, record: ProposalRecord) -> None:
        """A found repo must still be in scope, and reachable by the write token (#236).

        A listed repo isn't checked, so channels without scopes approve as before.
        """
        channel, repo = record.channel_id, record.proposal.repo
        if not self._repos.is_discovered(channel, repo):
            return
        try:
            self._repos.repo(channel, repo)
        except PolicyViolation as gone:
            raise Refused(REPO_GONE.format(reason=gone)) from None
        try:
            reachable = await self._repos.writable(channel, repo)
        except Exception as exc:
            log.warning("could not check write access to %s: %s", repo, type(exc).__name__)
            raise Refused(WRITE_UNCHECKED.format(repo=repo)) from None
        if not reachable:
            raise Refused(NOT_WRITABLE.format(repo=repo))

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

    async def _same_as(self, click: Click, record: ProposalRecord) -> str:
        match = _SAME_AS_VALUE.fullmatch(click.value)
        if match is None:
            raise Refused(NOT_THIS_CARD)
        await self._check_may_approve(click, record)
        try:
            marked = await self._store.mark_duplicate(
                record.id, index=int(match.group(3)), expected_hash=match.group(2), by=click.user_id
            )
        except ContentChangedError:
            await self._show(record)
            raise Refused(STALE_SAME_AS) from None
        except DraftClearedError:
            raise Refused(DISCARDED) from None
        except DuplicateRefusedError as refused:
            raise Refused(str(refused)) from None
        log.info("proposal %s marked a duplicate by user %s", record.id, click.user_id)
        await self._show(marked)
        return "same_as"

    async def _not_duplicate(self, click: Click, record: ProposalRecord) -> str:
        if click.value != record.id:
            raise Refused(NOT_THIS_CARD)
        await self._check_may_approve(click, record)
        try:
            undone = await self._store.undo_duplicate(record.id, by=click.user_id)
        except DuplicateRefusedError as refused:
            raise Refused(str(refused)) from None
        # The thread is drafted again, so a real issue isn't lost to a wrong click.
        await self._store.enqueue_run(RunKind.DRAFTING, undone.channel_id, undone.thread_ts)
        log.info("proposal %s: duplicate undone by user %s", record.id, click.user_id)
        await self._show(undone)
        return "not_duplicate"

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
    # A button sends value; a select menu sends selected_option.value.
    value = _str(action.get("value")) or _str(_map(action.get("selected_option")).get("value"))
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

    def __init__(
        self, client: Any, team_id: str | None = None, *, people: SlackPeople | None = None
    ) -> None:
        self._client = client
        self._people = people or SlackPeople(client, team_id)

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

    async def same_as(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("a same as", handler.on_same_as(body))

    async def not_duplicate(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("an undo of same as", handler.on_not_duplicate(body))

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
    app.action(ACTION_SAME_AS)(same_as)
    app.action(ACTION_NOT_DUPLICATE)(not_duplicate)
    app.action(ACTION_REQUEST_CHANGES)(request_changes)
    app.view(CALLBACK_CHANGES)(changes_submitted)
