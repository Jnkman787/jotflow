"""Add me: a person adds themselves as an assignee from a button (#305).

**On a pending or failed card,** Add me is an edit, as Make assignee is (#160),
through ``edit_proposal``. Any member may click it, approvers on or not, since
it adds only the clicker's own linked login, and changing a draft that still
needs approval changes nothing anyone has agreed to. The hash changes, so an
earlier Approve is stale.

**On a filed issue's reply,** a click makes an update adding exactly the
clicker's login, which no agent drafted. It's filed at once, through the normal
execution path as one hash-locked write with the clicker as approver, only when
all of these hold:

- the clicker's link is trusted, verified or admin-linked, and the account id it
  stores is the one GitHub gives for the login just now
- the channel is in open mode, or the clicker is an approver
- they've made fewer than 5 Add me requests in the past hour

Otherwise it goes on a card for an approver, never auto mode, and the card says
whose claim the login is.

**Someone with no link** gets a dialog asking for their GitHub login. JotFlow
checks, through the drafting runner, that the account exists, is a person, and
can be assigned issues in the repo, and that it's in ``[identity] github_org``
when that's set. The checks are limited to 10 in 10 minutes for each person,
since an answer says whether a login can reach the repo. The link is then stored
self-declared and unverified, as ``/jotflow link`` stores it, so a filed issue's
add from the dialog always goes on a card.

Every reply is fixed text JotFlow builds around a checked login, never anything
else the person typed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from jotflow.models import (
    Assignable,
    AssigneeReason,
    IssueChanges,
    Membership,
    ProposalRecord,
    ProposalState,
    RunKind,
    UpdateProposal,
)
from jotflow.orchestrator.approval import ApprovalHandler, Click, Refused
from jotflow.orchestrator.card_edits import assignees_of, with_assignees
from jotflow.orchestrator.cards import ACTION_ADD_ME, ACTION_ADD_ME_FILED
from jotflow.orchestrator.identity import clean_login
from jotflow.orchestrator.launch import GithubBusy, RunnerError
from jotflow.orchestrator.thread_notice import filed_issue
from jotflow.policy import Policy, PolicyViolation
from jotflow.repo_catalog import RepoCatalog
from jotflow.store.base import (
    EDITABLE_STATES,
    SELF_ADD_RUN,
    ConflictError,
    IdentityLink,
    Store,
    ToolCallRecord,
    TransitionError,
    utc_now,
)
from jotflow.store.hashing import content_hash

log = logging.getLogger("jotflow.add_me")

CALLBACK_ADD_ME = "jotflow_add_me_login"
LOGIN_BLOCK = "login"
LOGIN_INPUT = "login"

MAX_SELF_ADDS = 5
# The ledger's record of an Add me on a card, so it counts toward the same cap.
CARD_ADD = "add_me_card"
SELF_ADD_WINDOW = timedelta(hours=1)
MAX_CHECKS = 10
CHECK_WINDOW_SECONDS = 600.0
# GitHub's own limit on an issue's assignees.
MAX_ASSIGNEES = 10

_PROPOSAL_ID = re.compile(r"[0-9a-f]{16}")
_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")
_CHANNEL = re.compile(r"[CG][A-Z0-9]{2,24}")

NOT_EDITABLE = "This proposal can no longer be changed here."
NOT_THIS_REPLY = "This message no longer matches an issue JotFlow filed."
NOT_FILED = "This issue isn't filed yet, so use Add me on its card."
NO_TOPIC = "JotFlow can't add assignees to this issue from here. Ask in the thread instead."
REPO_GONE = "This channel can't change issues in that repo any more."
TOO_MANY_ASSIGNEES = "This proposal already has as many assignees as GitHub allows."
ALREADY_LINKED = "You've linked a GitHub login since, so click Add me again."
NO_RUNNER = "Checking a GitHub account needs JotFlow's agent containers, and they aren't on here."
BUSY = "GitHub couldn't be checked right now. Try again in a minute."
UNAVAILABLE = (
    "JotFlow couldn't reach GitHub through its agent container just now. "
    "Try again in a few minutes."
)
TOO_MANY_CHECKS = "That's 10 GitHub checks in 10 minutes. Try again later."
TOO_MANY_ADDS = "You've asked to be added 5 times in the past hour. Try again later."
NOT_A_LOGIN = (
    "That isn't a GitHub login. A login is 1 to 39 letters, digits or single hyphens, "
    "such as octo-example."
)
TAKEN = (
    "That login is linked to someone else in Slack. If it's yours, use /jotflow verify "
    "your-github-login to prove it, and the link moves to you."
)
CHECKING = "Checking GitHub…"
FAILED = "JotFlow couldn't finish this. Try Add me again in a moment."


def _no_account(login: str) -> str:
    return f"There's no GitHub account named {login}."


def _an_org(login: str) -> str:
    return f"{login} is a GitHub organization, not a person."


def _not_assignable(login: str, repo: str) -> str:
    return (
        f"{login} can't be assigned issues in {repo}. "
        "Ask a maintainer to add them to the organization or the repo."
    )


def _not_in_org(login: str, org: str) -> str:
    return f"{login} isn't a member of the {org} organization on GitHub."


def _org_hidden(login: str, org: str) -> str:
    return (
        f"JotFlow can't see whether {login} is a member of {org}. Whoever runs JotFlow "
        "needs to give its read token the organization's Members: Read-only permission."
    )


def _added_to_card(login: str) -> str:
    return f"Added {login} as an assignee on the card."


def _already_on_card(login: str) -> str:
    return f"{login} is already an assignee on the card."


def _linked(login: str) -> str:
    return (
        f"Linked your Slack account to GitHub login {login}, unverified. "
        "/jotflow verify proves it's yours, so later adds can be instant."
    )


def _filing(issue: str) -> str:
    return f"Adding you as an assignee of {issue} now."


def _asked(issue: str) -> str:
    return (
        f"Asked to add you to {issue}. A card in the thread waits for an approver. "
        "/jotflow verify makes later adds instant."
    )


def _already_asked(issue: str) -> str:
    return f"You've already asked to be added to {issue}. Its card is in the thread."


class PersonChecks(Protocol):
    """The drafting runner's person checks (#316). Tests use a fake."""

    async def assignable(self, owner: str, name: str, login: str) -> Assignable: ...

    async def membership(self, org: str, login: str) -> Membership: ...


class AddMeSlack(Protocol):
    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None: ...

    async def open_modal(self, trigger_id: str, view: Mapping[str, Any]) -> None: ...

    async def update_modal(self, view_id: str, view: Mapping[str, Any]) -> None: ...


Refresh = Callable[[str], Awaitable[object]]
ChecksFor = Callable[[], PersonChecks | None]


@dataclass(frozen=True)
class Target:
    """Where an Add me dialog came from: a card, by its ts, or a filed reply, by its thread.

    It rides in the dialog's private_metadata, and is checked against the store again
    on submit, never trusted.
    """

    kind: Literal["card", "filed"]
    proposal_id: str
    channel_id: str
    ts: str

    def encode(self) -> str:
        return json.dumps([self.kind, self.proposal_id, self.channel_id, self.ts])

    @staticmethod
    def decode(raw: object) -> Target | None:
        try:
            kind, proposal_id, channel_id, ts = json.loads(raw if isinstance(raw, str) else "")
        except (ValueError, TypeError):
            return None
        if (
            kind not in ("card", "filed")
            or not isinstance(proposal_id, str)
            or not _PROPOSAL_ID.fullmatch(proposal_id)
            or not isinstance(channel_id, str)
            or not _CHANNEL.fullmatch(channel_id)
            or not isinstance(ts, str)
            or not _TS.fullmatch(ts)
        ):
            return None
        return Target(kind, proposal_id, channel_id, ts)


@dataclass(frozen=True)
class Submitted:
    """A dialog that passed the quick checks, for the slower GitHub ones."""

    user_id: str
    view_id: str
    target: Target
    login: str


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text, "emoji": False}


def login_modal(target: Target, repo: str) -> dict[str, Any]:
    """The dialog asking an unlinked person for their GitHub login."""
    return {
        "type": "modal",
        "callback_id": CALLBACK_ADD_ME,
        "private_metadata": target.encode(),
        "title": _plain("Add me"),
        "submit": _plain("Link and add me"),
        "close": _plain("Cancel"),
        "blocks": [
            {
                "type": "input",
                "block_id": LOGIN_BLOCK,
                "label": _plain("Your GitHub login"),
                "hint": _plain(
                    f"JotFlow checks that the account can be assigned issues in {repo}. "
                    "The link is unverified until you prove it's yours with /jotflow verify."
                ),
                "element": {
                    "type": "plain_text_input",
                    "action_id": LOGIN_INPUT,
                    "max_length": 100,
                    "placeholder": _plain("octo-example"),
                },
            }
        ],
    }


def result_modal(text: str) -> dict[str, Any]:
    """What the dialog shows once JotFlow has checked: fixed text, closed with Done."""
    return {
        "type": "modal",
        "title": _plain("Add me"),
        "close": _plain("Done"),
        "blocks": [{"type": "section", "text": _plain(text)}],
    }


def _typed_login(view: Mapping[str, Any]) -> str:
    values = _map(_map(view.get("state")).get("values"))
    return _str(_map(_map(values.get(LOGIN_BLOCK)).get(LOGIN_INPUT)).get("value")) or ""


def _thread_of(body: Mapping[str, Any]) -> str | None:
    """The thread of the message a click was on, as Slack's payload names it."""
    container = _map(body.get("container"))
    message = _map(body.get("message"))
    thread = _str(container.get("thread_ts")) or _str(message.get("thread_ts"))
    return thread if thread and _TS.fullmatch(thread) else None


class AddMe:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        gate: ApprovalHandler,
        slack: AddMeSlack,
        checks: ChecksFor,
        *,
        refresh: Refresh | None = None,
        repos: RepoCatalog | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self._store = store
        self._policy = policy
        self._gate = gate
        self._slack = slack
        self._checks = checks
        self._refresh = refresh
        self._repos = repos if repos is not None else RepoCatalog.static(policy)
        self._clock = clock
        self._monotonic = monotonic
        self._new_id = new_id
        self._recent: dict[str, deque[float]] = {}
        # One filed-issue request at a time per person, so two clicks can't both pass
        # the cap and the one-waiting-request check (CodeRabbit).
        self._asking: dict[str, asyncio.Lock] = {}

    # The buttons

    async def on_card(self, body: Mapping[str, Any]) -> str:
        """Add me on a pending or failed card."""
        return await self._gate.guarded(body, self._card)

    async def on_filed(self, body: Mapping[str, Any]) -> str:
        """Add me on the reply that says where an issue was filed."""
        thread = _thread_of(body)

        async def act(click: Click) -> str:
            return await self._filed(click, thread)

        return await self._gate.member_click(body, act)

    async def _card(self, click: Click, record: ProposalRecord) -> str:
        self._check_editable(record)
        link = await self._store.get_identity(click.user_id)
        if link is None:
            assert record.card_ts is not None  # the gate matched the click to the card
            target = Target("card", record.id, record.channel_id, record.card_ts)
            return await self._ask(click, target, record.proposal.repo)
        text, outcome = await self._add_to_card(click.user_id, record, link.github_login)
        await self._tell(click.user_id, record.channel_id, text)
        return outcome

    async def _filed(self, click: Click, thread: str | None) -> str:
        record = await self._filed_record(click.value, click.channel_id, thread)
        repo, _ = self._issue(record)
        link = await self._store.get_identity(click.user_id)
        if link is None:
            target = Target("filed", record.id, record.channel_id, record.thread_ts)
            return await self._ask(click, target, repo)
        text, outcome = await self._self_add(click.user_id, record, link, entered=False)
        await self._tell(click.user_id, record.channel_id, text)
        return outcome

    async def _ask(self, click: Click, target: Target, repo: str) -> str:
        if click.trigger_id is None:
            raise Refused(NOT_EDITABLE)
        await self._slack.open_modal(click.trigger_id, login_modal(target, repo))
        log.info("add me: user %s asked for a GitHub login", click.user_id)
        return "asked"

    # The dialog

    async def check_submitted(self, body: Mapping[str, Any]) -> Submitted | str:
        """The quick checks, within Slack's three seconds: who, where, and the login's
        shape and owner. Returns what to check on GitHub, or the error for the dialog."""
        user_id = _str(_map(body.get("user")).get("id")) or ""
        view = _map(body.get("view"))
        target = Target.decode(view.get("private_metadata"))
        view_id = _str(view.get("id"))
        try:
            if target is None or view_id is None:
                raise Refused(NOT_EDITABLE)
            await self._gate.check_member(body, user_id, target.channel_id)
            await self._target_record(target)
            login = clean_login(_typed_login(view))
            if login is None:
                raise Refused(NOT_A_LOGIN)
            if await self._store.get_identity(user_id) is not None:
                raise Refused(ALREADY_LINKED)
            holder = await self._store.identity_for_login(login)
            if holder is not None and holder.slack_user_id != user_id:
                raise Refused(TAKEN)
        except Refused as refused:
            log.info("add me: a login was refused for user %s", user_id or "?")
            return str(refused)
        return Submitted(user_id, view_id, target, login)

    async def finish_submitted(self, submitted: Submitted) -> str:
        """The GitHub checks, the link, and the add. Shows the outcome in the dialog."""
        try:
            text = await self._finish(submitted)
        except Refused as refused:
            log.info("add me: the dialog was refused for user %s", submitted.user_id)
            text = str(refused)
        except Exception:
            # Never left on "Checking GitHub…": the dialog always gets an answer.
            log.exception("add me: the dialog failed for user %s", submitted.user_id)
            text = FAILED
        await self._slack.update_modal(submitted.view_id, result_modal(text))
        return text

    async def _finish(self, submitted: Submitted) -> str:
        user, login, target = submitted.user_id, submitted.login, submitted.target
        record = await self._target_record(target)
        repo = record.proposal.repo if target.kind == "card" else self._issue(record)[0]
        if target.kind == "filed":
            await self._check_may_ask(user, record)
        found = await self._check_account(user, repo, login)
        try:
            link = await self._store.link_identity(user, login)
        except ConflictError:
            raise Refused(TAKEN) from None
        log.info("add me: user %s linked a GitHub login from the dialog", user)
        if target.kind == "card":
            record = await self._target_record(target)
            text, _ = await self._add_to_card(user, record, link.github_login)
        else:
            text, _ = await self._self_add(user, record, link, entered=True, found=found)
        return f"{_linked(link.github_login)} {text}"

    async def _target_record(self, target: Target) -> ProposalRecord:
        if target.kind == "card":
            record = await self._store.get_proposal(target.proposal_id)
            if (
                record is None
                or record.channel_id != target.channel_id
                or record.card_ts != target.ts
            ):
                raise Refused(NOT_EDITABLE)
            self._check_editable(record)
            return record
        return await self._filed_record(target.proposal_id, target.channel_id, target.ts)

    # A card

    def _check_editable(self, record: ProposalRecord) -> None:
        if record.state not in EDITABLE_STATES or record.draft_cleared_at is not None:
            raise Refused(NOT_EDITABLE)

    async def _add_to_card(self, user: str, record: ProposalRecord, login: str) -> tuple[str, str]:
        logins = assignees_of(record.proposal)
        if login.casefold() in {existing.casefold() for existing in logins}:
            return _already_on_card(login), "unchanged"
        if len(logins) >= MAX_ASSIGNEES:
            raise Refused(TOO_MANY_ASSIGNEES)
        await self._check_add_cap(user)
        edited = with_assignees(record.proposal, [*logins, login], added=login)
        try:
            await self._store.edit_proposal(record.id, edited, by=user, field="assign")
        except TransitionError:
            raise Refused(NOT_EDITABLE) from None
        now = self._clock()
        with contextlib.suppress(ConflictError):
            await self._store.record_tool_call(
                ToolCallRecord(
                    run_id=_card_adds_run(user),
                    tool=CARD_ADD,
                    args_hash=f"{record.id}:{now.isoformat()}",
                    channel_id=record.channel_id,
                    thread_ts=record.thread_ts,
                    result_json=json.dumps({"at": now.isoformat()}),
                    called_at=now,
                )
            )
        log.info("proposal %s: user %s added themselves on the card", record.id, user)
        if self._refresh is not None:
            await self._refresh(record.id)
        return _added_to_card(login), "added"

    # A filed issue

    async def _filed_record(
        self, proposal_id: str, channel_id: str | None, thread: str | None
    ) -> ProposalRecord:
        record = (
            await self._store.get_proposal(proposal_id)
            if _PROPOSAL_ID.fullmatch(proposal_id)
            else None
        )
        if (
            record is None
            or channel_id != record.channel_id
            or thread is None
            or thread != record.thread_ts
        ):
            raise Refused(NOT_THIS_REPLY)
        if record.state is not ProposalState.APPLIED:
            raise Refused(NOT_FILED)
        self._issue(record)
        if record.proposal.topic_id is None:
            raise Refused(NO_TOPIC)
        return record

    def _issue(self, record: ProposalRecord) -> tuple[str, int]:
        """The filed issue, from the URL ``record_result`` checked, in a repo the channel
        can still change."""
        found = filed_issue(record)
        if found is None:
            raise Refused(NOT_THIS_REPLY)
        try:
            repo = self._repos.repo(record.channel_id, found[0]).repo
        except PolicyViolation:
            raise Refused(REPO_GONE) from None
        return repo, found[1]

    def _label(self, record: ProposalRecord) -> str:
        repo, number = self._issue(record)
        for entry in self._repos.repos(record.channel_id):
            if entry.repo.casefold() == repo.casefold() and entry.nickname:
                return f"{entry.nickname}#{number}"
        return f"{repo}#{number}"

    async def _check_may_ask(self, user: str, record: ProposalRecord) -> None:
        """At most one waiting request per person and issue, and 5 an hour."""
        repo, number = self._issue(record)
        for other in await self._store.thread_proposals(record.channel_id, record.thread_ts):
            draft = other.proposal
            if (
                other.run_id == SELF_ADD_RUN
                and other.state is ProposalState.PENDING
                and user in other.participants
                and isinstance(draft, UpdateProposal)
                and draft.repo.casefold() == repo.casefold()
                and draft.issue_number == number
            ):
                raise Refused(_already_asked(self._label(record)))
        await self._check_add_cap(user)

    async def _adds_in_window(self, user: str) -> int:
        """A person's Add me requests in the past hour: on filed issues and on cards."""
        since = self._clock() - SELF_ADD_WINDOW
        filed = await self._store.count_self_adds(user, since)
        # Counted by the store, within the window only, never the whole history.
        on_cards = await self._store.count_run_calls(_card_adds_run(user), CARD_ADD, since=since)
        return filed + on_cards

    async def _check_add_cap(self, user: str) -> None:
        # One cap for both kinds of Add me (#344 review), so cards aren't a way around it.
        if await self._adds_in_window(user) >= MAX_SELF_ADDS:
            raise Refused(TOO_MANY_ADDS)

    async def _self_add(
        self,
        user: str,
        record: ProposalRecord,
        link: IdentityLink,
        *,
        entered: bool,
        found: Assignable | None = None,
    ) -> tuple[str, str]:
        """Make the update adding ``user``'s own login, and file it at once only when
        their link is trusted and matches GitHub, and they may approve here."""
        async with self._asking.setdefault(user, asyncio.Lock()):
            return await self._self_add_locked(user, record, link, entered=entered, found=found)

    async def _self_add_locked(
        self,
        user: str,
        record: ProposalRecord,
        link: IdentityLink,
        *,
        entered: bool,
        found: Assignable | None,
    ) -> tuple[str, str]:
        repo, number = self._issue(record)
        label = self._label(record)
        await self._check_may_ask(user, record)
        if found is None:
            found = await self._check_account(user, repo, link.github_login)
        topic = record.proposal.topic_id
        assert topic is not None  # _filed_record refuses an issue without one
        login = link.github_login
        reason = AssigneeReason(login=login, why="added_on_card")
        draft = UpdateProposal(
            topic_id=topic,
            repo=repo,
            issue_number=number,
            changes=IssueChanges(add_assignees=[login], assignee_reasons=[reason]),
            confidence=1.0,
        )
        stored = await self._store.add_proposal(
            ProposalRecord(
                id=self._new_id(),
                channel_id=record.channel_id,
                thread_ts=record.thread_ts,
                run_id=SELF_ADD_RUN,
                proposal=draft,
                participants=[user],
                created_at=self._clock(),
            )
        )
        approval = self._policy.approval
        may_approve = not approval.approvers_enabled or user in approval.approvers
        trusted = link.trusted and link.github_id == found.id
        # With a second approver required, nobody files their own request (#344 review):
        # it always waits on a card, approver or not, and someone else approves it.
        second = approval.require_second_approver
        if entered or not trusted or not may_approve or second:
            log.info("proposal %s: user %s's Add me waits on a card", stored.id, user)
            return _asked(label), "carded"
        try:
            approved = await self._store.transition(
                stored.id,
                ProposalState.APPROVED,
                expected={ProposalState.PENDING},
                approved_by=user,
                expected_hash=content_hash(draft),
            )
        except TransitionError:
            return _asked(label), "carded"
        await self._store.enqueue_run(
            RunKind.EXECUTION, approved.channel_id, approved.thread_ts, proposal_id=approved.id
        )
        log.info("proposal %s: user %s added themselves; execution queued", approved.id, user)
        return _filing(label), "filing"

    # GitHub, through the drafting runner

    def _limit(self, user: str, calls: int) -> None:
        now = self._monotonic()
        recent = self._recent.setdefault(user, deque())
        while recent and now - recent[0] >= CHECK_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) + calls > MAX_CHECKS:
            raise Refused(TOO_MANY_CHECKS)
        recent.extend([now] * calls)

    async def _check_account(self, user: str, repo: str, login: str) -> Assignable:
        checks = self._checks()
        if checks is None:
            raise Refused(NO_RUNNER)
        org = self._policy.identity.github_org
        self._limit(user, 2 if org else 1)
        owner, _, name = repo.partition("/")
        try:
            found = await checks.assignable(owner, name, login)
            if found.account == "missing":
                raise Refused(_no_account(login))
            if found.account == "organization":
                raise Refused(_an_org(login))
            if not found.assignable:
                raise Refused(_not_assignable(login, repo))
            if org is not None:
                member = await checks.membership(org, login)
                if member.member == "no":
                    raise Refused(_not_in_org(login, org))
                if member.member == "hidden":
                    raise Refused(_org_hidden(login, org))
        except GithubBusy:
            raise Refused(BUSY) from None
        except RunnerError as failed:
            log.warning("add me: the runner failed: %s", failed)
            raise Refused(UNAVAILABLE) from None
        return found

    async def _tell(self, user_id: str, channel_id: str, text: str) -> None:
        await self._slack.ephemeral(channel_id, user_id, text)


class BoltAddMeSlack:
    """:class:`AddMeSlack` over slack-sdk's client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def ephemeral(self, channel_id: str, user_id: str, text: str) -> None:
        await self._client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)

    async def open_modal(self, trigger_id: str, view: Mapping[str, Any]) -> None:
        await self._client.views_open(trigger_id=trigger_id, view=dict(view))

    async def update_modal(self, view_id: str, view: Mapping[str, Any]) -> None:
        await self._client.views_update(view_id=view_id, view=dict(view))


def _card_adds_run(user: str) -> str:
    return f"add_me:{user}"


def register_add_me(app: Any, add_me: AddMe) -> None:
    """Route both Add me buttons and the login dialog. Each acks first."""

    async def run(name: str, work: Awaitable[object]) -> None:
        try:
            await work
        except Exception:
            log.exception("add me: %s failed", name)

    async def card(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("a card click", add_me.on_card(body))

    async def filed(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await run("a reply click", add_me.on_filed(body))

    async def submitted(ack: Any, body: dict[str, Any]) -> None:
        try:
            checked = await add_me.check_submitted(body)
        except Exception:
            log.exception("add me: could not check a login")
            checked = "JotFlow could not check this. Try again in a moment."
        if isinstance(checked, str):
            await ack(response_action="errors", errors={LOGIN_BLOCK: checked})
            return
        # Acknowledge first, within Slack's three seconds, then ask GitHub.
        await ack(response_action="update", view=result_modal(CHECKING))
        await run("a login", add_me.finish_submitted(checked))

    app.action(ACTION_ADD_ME)(card)
    app.action(ACTION_ADD_ME_FILED)(filed)
    app.view(CALLBACK_ADD_ME)(submitted)
