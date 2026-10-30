"""Keep every proposal's card in Slack in step with the store (#152).

The store is the card's outbox. Every change a card shows raises the
proposal's revision, and the card records which revision it shows. One loop,
:meth:`CardSync.sync`, asks the store for the cards that are missing or behind,
posts or edits each one, and records what it drew. So nothing that writes a
proposal calls Slack. ``propose_issue`` stays a pure store write that a Slack
outage cannot fail, and a card that could not be drawn stays due and is tried
again, across restarts too.

A proposal superseded or rejected before its card went up never gets one.
Every other state is drawn: a pending card, an auto-approved one (#28), and
each later state edited in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

import anyio

from jotflow.board_catalog import BoardCatalog
from jotflow.execution.attribution import Attribution, credit, shown_credits
from jotflow.models import ProposalRecord, ProposalState, RepoVisibility
from jotflow.orchestrator.cards import CardContext, CardPublisher, Visibility
from jotflow.policy import IssuesPolicy, Policy, UnknownChannelError
from jotflow.repo_catalog import RepoCatalog
from jotflow.store.base import AUTO_APPROVER, IdentityLink, Store, utc_now
from jotflow.whiteboard.doc_tools import PASSAGES, READ_TOOL, card_lines

log = logging.getLogger("jotflow.cards")

SYNC_SECONDS = 1.0
BATCH = 50
FIRST_RETRY = timedelta(seconds=2)
LAST_RETRY = timedelta(minutes=1)

# A proposal that ended before anyone saw it needs no card.
_NO_CARD_NEEDED = frozenset({ProposalState.SUPERSEDED, ProposalState.REJECTED})
# Auto mode posts its summary card after the write (#28): not while it is under way.
_WRITING = frozenset({ProposalState.APPROVED, ProposalState.EXECUTING, ProposalState.RECONCILING})
AUTO = AUTO_APPROVER

# Slack errors that retrying cannot fix: the card's message or its channel is gone.
FINAL_SLACK_ERRORS = frozenset(
    {
        "message_not_found",
        "channel_not_found",
        "is_archived",
        "not_in_channel",
        "cant_update_message",
    }
)


async def _own_reads(
    store: Store, record: ProposalRecord, read: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """The run's doc reads this card lists (#374).

    A run that drafts for several repos, such as a split message's topics, reads
    docs before any draft exists, so a read can't be pinned to one draft. A card
    leaves out only reads from a repo another of the run's drafts is for, so
    each split card shows its own repo's reads, and no read is left off every card.
    """
    own = record.proposal.repo.casefold()
    others = {
        p.proposal.repo.casefold()
        for p in await store.run_proposals(record.run_id)
        if p.id != record.id
    } - {own}
    return [(repo, path) for repo, path in read if repo.casefold() not in others]


def _slack_error(exc: Exception) -> str | None:
    """The error code of a Slack API error, such as ``message_not_found``."""
    response = getattr(exc, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except Exception:
        return None
    return code if isinstance(code, str) else None


class People(Protocol):
    """Display names for Slack users. Tests use a fake."""

    async def name(self, user_id: str) -> str | None: ...


def effective_visibility(policy: RepoVisibility | None, agent: RepoVisibility | None) -> Visibility:
    """What the card says about a repo's visibility (D7, #122).

    The policy is authoritative; the agent may only add a warning. Either
    saying public warns. Only the policy can say private, so an agent's
    private, alone or against the policy's public, never removes anything.
    """
    if policy == "public" or agent == "public":
        return "public"
    if policy == "private":
        return "private"
    return "unknown"


# How long the card waits for the credited names before saying they couldn't be checked.
CREDIT_SECONDS = 5.0
CREDIT_UNKNOWN = "Credited in the issue: couldn't be checked just now"


async def _credit_line(
    policy: Policy, store: Store, attribution: Attribution | None, record: ProposalRecord
) -> tuple[str | None, str | None]:
    """The names the footer will carry, by the footer's own rules (#307 review), and
    their Slack ids as JSON, for the card to lock (#396). Once approved, only the ids
    the approvable card showed, as the write uses. When the names can't be checked,
    no one: the footer then credits nobody rather than names the card didn't show."""
    if attribution is None or policy.issue_links == "none":
        return None, None
    approvable = record.state in (ProposalState.PENDING, ProposalState.FAILED)
    allowed = None if approvable else shown_credits(record)
    with anyio.move_on_after(CREDIT_SECONDS):
        try:
            found = await credit(record, attribution, store, allowed)
        except Exception as exc:
            log.warning("card %s: credited names unknown (%s)", record.id, type(exc).__name__)
            return CREDIT_UNKNOWN, "[]"
        return found.line(record.proposal.kind == "update_issue"), json.dumps(list(found.ids))
    return CREDIT_UNKNOWN, "[]"


async def card_context(
    policy: Policy,
    store: Store,
    people: People,
    record: ProposalRecord,
    repos: RepoCatalog | None = None,
    attribution: Attribution | None = None,
    boards: BoardCatalog | None = None,
) -> CardContext:
    """Everything the card shows besides the proposal: names, identities, the channel."""
    users = list(dict.fromkeys(record.participants))
    if record.approved_by and record.approved_by != AUTO:
        users.append(record.approved_by)
    names: dict[str, str] = {}
    identities: dict[str, IdentityLink] = {}
    for user in users:
        if (name := await people.name(user)) is not None:
            names[user] = name
        if (link := await store.get_identity(user)) is not None:
            identities[user] = link
    channel = policy.channel(record.channel_id)
    board_catalog = boards if boards is not None else BoardCatalog.static(policy)
    catalog = repos if repos is not None else RepoCatalog.static(policy)
    channel_repos = catalog.repos(record.channel_id)
    repo = record.proposal.repo
    # A found repo says "private" only when GitHub did, so anything else warns (#236).
    configured = next(
        (r.visibility for r in channel_repos if r.repo.casefold() == repo.casefold()), None
    )
    if configured is None and catalog.is_discovered(record.channel_id, repo):
        configured = "public"  # in no listing now: warn rather than guess
    seen = effective_visibility(configured, record.proposal.repo_visibility)
    # The docs the drafting run read, by path and sha only (#209).
    docs_read, docs_copied = card_lines(
        await store.tool_call_results(record.run_id, (READ_TOOL,)),
        await store.tool_call_results(record.run_id, (PASSAGES,)),
        record.id,
    )
    docs_read = await _own_reads(store, record, docs_read)
    credit_line, credits = await _credit_line(policy, store, attribution, record)
    return CardContext(
        channel=channel,
        credit_line=credit_line,
        credits=credits,
        # What approving now would lock while it can be approved or retried; after
        # that, what the approval locked (#306 follow-up).
        issues=policy.issues
        if record.state in (ProposalState.PENDING, ProposalState.FAILED)
        else IssuesPolicy.from_snapshot(record.approved_issues, policy.issues),
        names=names,
        identities=identities,
        visibility={repo: seen} if seen != "unknown" else {},
        repos=channel_repos,
        docs_read=tuple(docs_read),
        docs_copied=tuple(docs_copied),
        revisions=tuple(await store.proposal_revisions(record.id)),
        boards=tuple(board_catalog.boards(record.channel_id)),
        offered_boards=tuple(board_catalog.offered(record.channel_id)),
        source_skipped=await _source_skipped(store, record),
    )


async def _source_skipped(store: Store, record: ProposalRecord) -> bool:
    """Whether an unsplit dropped this card because a person marked its message not
    an idea, so the card says that rather than that it was moved."""
    if record.unsplit_at is None:
        return False
    for ref in record.proposal.sources:
        link = await store.get_message(ref.channel_id, ref.ts)
        if link is not None and link.chatter and link.linked_by == "person":
            return True
    return False


# Settled states after which a lost follow-up is drafted again (#304): written, or
# turned down. A failed one only once discarded, since a retry may still write it.
_FOLLOW_UP_STATES = frozenset(
    {ProposalState.APPLIED, ProposalState.REJECTED, ProposalState.SUPERSEDED}
)


class CardSync:
    """Posts each due card, or edits it in place, and records what it drew."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        publisher: CardPublisher,
        people: People,
        *,
        clock: Callable[[], datetime] = utc_now,
        repos: RepoCatalog | None = None,
        arm_thread: Callable[[str, str], Awaitable[None]] | None = None,
        boards: BoardCatalog | None = None,
        attribution: Attribution | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._repos = repos
        # Who the issue will credit, so the card shows it and locks it (#396).
        self._attribution = attribution
        # Read at each draw: the start-up check and the board scopes change it (#309).
        self._boards = boards
        self._arm = arm_thread
        self._publisher = publisher
        self._people = people
        self._clock = clock
        # proposal id -> (when to try next, how long the last wait was)
        self._waiting: dict[str, tuple[datetime, timedelta]] = {}
        # One draw at a time, so sync and refresh never both post the same new card.
        self._lock = asyncio.Lock()

    async def sync(self) -> int:
        """Draw every due card that is not waiting to retry. Returns how many were drawn."""
        drawn = 0
        now = self._clock()
        # Ask for enough that proposals waiting to retry cannot crowd out the rest.
        for record in await self._store.cards_due(BATCH + len(self._waiting)):
            wait = self._waiting.get(record.id)
            if wait is not None and wait[0] > now:
                continue
            if await self._draw(record):
                drawn += 1
        return drawn

    async def redraw(self, proposal_id: str) -> bool:
        """Draw one proposal's card now even if the store says it's current, such as
        when jotflow.toml's [issues] changed since it was drawn (#306 follow-up)."""
        async with self._lock:
            record = await self._store.get_proposal(proposal_id)
            if record is None:
                return False
            return await self._draw_now(record)

    async def refresh(self, proposal_id: str) -> bool:
        """Draw one proposal's card now, if it is due. For callers that just changed it."""
        record = await self._store.get_proposal(proposal_id)
        if record is None:
            return False
        return await self._draw(record)

    async def _draw(self, seen: ProposalRecord) -> bool:
        async with self._lock:
            # Read it again under the lock: another draw may have just posted it.
            record = await self._store.get_proposal(seen.id)
            if record is None or not record.card_due:
                return False
            drawn = await self._draw_now(record)
        await self._follow_up(record)
        return drawn

    async def _follow_up(self, record: ProposalRecord) -> None:
        """A revision lost a race to this proposal settling (#304): once it's filed or
        turned down, draft its thread again, so the follow-up isn't lost."""
        discarded = record.state is ProposalState.FAILED and record.draft_cleared_at is not None
        if (record.state not in _FOLLOW_UP_STATES and not discarded) or self._arm is None:
            return
        try:
            if await self._store.take_follow_up_due(record.id):
                await self._arm(record.channel_id, record.thread_ts)
        except Exception as exc:
            log.warning(
                "proposal %s: could not queue its follow-up (%s)", record.id, type(exc).__name__
            )

    async def _draw_now(self, record: ProposalRecord) -> bool:
        try:
            if record.card_ts is None and record.state in _NO_CARD_NEEDED:
                await self._store.mark_card_shown(record.id, record.revision)
                return False
            if record.card_ts is None and record.approved_by == AUTO and record.state in _WRITING:
                return False  # stays due: posted once the write is applied or failed
            context = await card_context(
                self._policy,
                self._store,
                self._people,
                record,
                self._repos,
                self._attribution,
                boards=self._boards,
            )
            await self._publisher.show(record, context)
            # What [issues] the card showed, so an approval can check it (#306 follow-up).
            await self._store.mark_card_shown(
                record.id, record.revision, context.issues.snapshot(), context.credits
            )
        except UnknownChannelError:
            # jotflow.toml no longer lists the channel: there is nowhere to draw it.
            log.warning("proposal %s: its channel is not configured; no card", record.id)
            await self._store.mark_card_shown(record.id, record.revision)
            return False
        except Exception as exc:
            if (code := _slack_error(exc)) in FINAL_SLACK_ERRORS:
                # Someone deleted the card, or the channel is gone: never post it again.
                log.warning(
                    "proposal %s: Slack said %s; the card is left as it is", record.id, code
                )
                self._waiting.pop(record.id, None)
                await self._store.mark_card_shown(record.id, record.revision)
                return False
            self._back_off(record.id)
            # The type only: an error's text could quote the card.
            log.warning("proposal %s: could not draw its card (%s)", record.id, type(exc).__name__)
            return False
        self._waiting.pop(record.id, None)
        log.info("proposal %s: card shows revision %d", record.id, record.revision)
        return True

    def _back_off(self, proposal_id: str) -> None:
        last = self._waiting.get(proposal_id)
        wait = FIRST_RETRY if last is None else min(last[1] * 2, LAST_RETRY)
        self._waiting[proposal_id] = (self._clock() + wait, wait)


class BoltPeople:
    """:class:`People` over slack-sdk's client, cached for the life of the process."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._names: dict[str, str | None] = {}

    async def name(self, user_id: str) -> str | None:
        if user_id not in self._names:
            try:
                response = await self._client.users_info(user=user_id)
            except Exception as exc:
                log.warning("could not look up a Slack user (%s)", type(exc).__name__)
                return None
            self._names[user_id] = _display_name(response.get("user") or {})
        return self._names[user_id]


def _display_name(user: Mapping[str, Any]) -> str | None:
    profile = user.get("profile") or {}
    for value in (profile.get("display_name"), profile.get("real_name"), user.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
