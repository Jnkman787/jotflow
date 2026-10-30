"""Who raised an approved proposal, who added to it, and where in Slack, for its
issue footer (#31, #307).

The execution packet never carries thread text. The footer needs only the
thread root's author (as a user id, from the same lookup approval uses), the
authors of the messages the idea is drawn from, their display names, and the
workspace URL to build permalinks. Each lookup may fail or be slow; then that
part of the footer is left out, and the write still goes ahead.

A new issue credits everyone who contributed: the authors of its cited sources and
of every other message linked to its topic in the same thread. An update credits
only who added the new information, the authors of its own sources. Every author is
one Slack confirms wrote the message, never the agent's participants list.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

import anyio

from jotflow.execution.writes import _credited, _joined, clean_name, footer, permalink
from jotflow.models import MessageRef, ProposalRecord
from jotflow.policy import Policy
from jotflow.store.base import AUTO_APPROVER, Store

log = logging.getLogger("jotflow.execution")

# The whole people phase of an execution get_work, inside the 30 s request timeout.
ATTRIBUTION_SECONDS = 10.0
# The most messages whose authors are looked up for one footer, one Slack call each.
MAX_CREDITED_MESSAGES = 30


class Attribution(Protocol):
    async def authors(self, refs: Sequence[MessageRef]) -> set[str]: ...

    async def name(self, user_id: str) -> str | None: ...

    async def workspace_url(self) -> str | None: ...


async def _authors_of(attribution: Attribution, refs: Sequence[MessageRef]) -> list[str]:
    """Who wrote each message, in order, skipping any Slack cannot confirm."""
    found: list[str] = []
    for ref in refs[:MAX_CREDITED_MESSAGES]:
        try:
            authors = await attribution.authors([ref])
        except LookupError:
            continue  # deleted or never there: nobody is named for it
        found += sorted(a for a in authors if a not in found)
    return found


async def _cutoff(record: ProposalRecord, store: Store) -> float:
    """The latest message time a proposal credits: when it was drafted (#307 review),
    or when it was last revised, so a revision's new authors are credited (#304).

    The card computes the credited names the same way, so the names filed are
    names an approver saw. A message added after that reaches the issue through
    an update, or a revision, which credits it then.
    """
    revisions = await store.proposal_revisions(record.id)
    latest = record.created_at
    if revisions and revisions[-1].revised_at > latest:
        latest = revisions[-1].revised_at
    return latest.timestamp()


async def _contributed(record: ProposalRecord, store: Store | None) -> list[MessageRef]:
    """The messages a new issue credits: its sources, then the other messages linked to
    its topic in the same thread, in time order, none newer than the draft. Never
    another thread's or another topic's."""
    refs = list(record.proposal.sources)
    if store is None:
        return refs
    # The draft's own topic when it names one, so another topic in the thread never
    # lends it contributors. Only a draft for a new topic looks at its sources' links.
    topics: set[str] = set()
    if record.proposal.topic_id:
        topics.add(record.proposal.topic_id)
    else:
        for source in record.proposal.sources:
            link = await store.get_message(source.channel_id, source.ts)
            if link is not None and link.topic_id is not None:
                topics.add(link.topic_id)
    seen = {ref.ts for ref in refs}
    latest = await _cutoff(record, store)
    extra: list[MessageRef] = []
    for topic_id in sorted(topics):
        found = await store.get_topic(topic_id)
        if found is None or found.topic.channel_id != record.channel_id:
            continue
        for ref in found.topic.messages:
            in_thread = (ref.thread_ts or ref.ts) == record.thread_ts
            if (
                in_thread
                and ref.channel_id == record.channel_id
                and ref.ts not in seen
                and float(ref.ts) <= latest
            ):
                seen.add(ref.ts)
                extra.append(ref)
    return refs + sorted(extra, key=lambda ref: float(ref.ts))


@dataclass(frozen=True)
class Credit:
    """Who a proposal's footer names: the poster of a new issue, then everyone else."""

    poster: str | None = None
    others: tuple[str, ...] = ()
    # The Slack ids behind those names (#396), so the card can lock who it showed.
    ids: tuple[str, ...] = ()

    def line(self, update: bool) -> str | None:
        """The card's line naming them (#307 review), or None if nobody would be named.

        It uses the footer's own rules, so the card shows exactly what's filed: each
        name once without case, the poster not repeated, and ten names at most.
        """
        if update:
            added = _credited(self.others)
            return f"Credited in the update: {_joined(added)}" if added else None
        poster = clean_name(self.poster) if self.poster else ""
        helped = _credited(self.others, skip=poster)
        if not poster and not helped:
            return None
        parts = [f"raised by {poster}"] if poster else []
        if helped:
            parts.append(f"with contributions from {_joined(helped)}")
        return "Credited in the issue: " + ", ".join(parts)


async def credit(
    record: ProposalRecord,
    attribution: Attribution,
    store: Store | None = None,
    allowed: Collection[str] | None = None,
) -> Credit:
    """Who the footer names, the same for the card and the write (#307).

    Only people Slack confirms wrote a credited message, never the agent-written
    participants list (#156 review). A new issue credits its poster and its topic's
    contributors; an update credits its own sources, who added the new information.
    Each name is looked up once. A failed lookup leaves that person out. With
    ``allowed``, only those Slack ids are credited: the ones the card showed (#396).
    """
    update = record.proposal.kind == "update_issue"
    names: dict[str, str | None] = {}

    async def name(user: str) -> str | None:
        if user not in names:
            names[user] = await attribution.name(user)
        return names[user]

    poster_id: str | None = None
    poster: str | None = None
    if not update:
        root = MessageRef(channel_id=record.channel_id, ts=record.thread_ts)
        try:
            poster_id = next(iter(await attribution.authors([root])), None)
        except LookupError:
            poster_id = None  # a deleted root names no one
        if poster_id is not None and allowed is not None and poster_id not in allowed:
            poster_id = None
        if poster_id is not None:
            poster = await name(poster_id)
    refs = list(record.proposal.sources) if update else await _contributed(record, store)
    others: list[str] = []
    ids = [poster_id] if poster_id is not None and poster is not None else []
    for user in await _authors_of(attribution, refs):
        if allowed is not None and user not in allowed:
            continue
        if user != poster_id and (found := await name(user)):
            others.append(found)
            ids.append(user)
    return Credit(poster=poster, others=tuple(others), ids=tuple(dict.fromkeys(ids)))


def shown_credits(record: ProposalRecord) -> frozenset[str] | None:
    """Whom the footer may credit (#396): the Slack ids the approved card showed. None
    for an auto-approved proposal, which no person approved from a card. A proposal
    with none recorded credits no one, rather than names nobody saw."""
    if record.approved_by == AUTO_APPROVER:
        return None
    if record.card_credits is None:
        return frozenset()
    try:
        found = json.loads(record.card_credits)
    except ValueError:
        return frozenset()
    if not isinstance(found, list):
        return frozenset()
    return frozenset(user for user in found if isinstance(user, str))


async def execution_footer(
    record: ProposalRecord,
    policy: Policy,
    attribution: Attribution | None,
    store: Store | None = None,
) -> str:
    """The footer for ``record``'s writes. Never raises for a failed Slack lookup."""
    if policy.issue_links == "none":
        return footer(record.id, poster=None, issue_links="none")
    update = record.proposal.kind == "update_issue"
    credited = Credit()
    base: str | None = None
    if attribution is not None:
        # The workspace URL first, so a slow Slack can cost names but never the links.
        with anyio.move_on_after(ATTRIBUTION_SECONDS / 4):
            try:
                base = await attribution.workspace_url()
            except Exception as exc:
                log.warning("proposal %s: no workspace URL (%s)", record.id, type(exc).__name__)
        with anyio.move_on_after(ATTRIBUTION_SECONDS):
            try:
                credited = await credit(record, attribution, store, shown_credits(record))
            except Exception as exc:  # a lookup failure leaves that part out
                log.warning(
                    "proposal %s: attribution incomplete (%s)", record.id, type(exc).__name__
                )
    links: list[str] = []
    for source in record.proposal.sources:
        if source.permalink:
            links.append(source.permalink)
        elif base:
            links.append(permalink(base, source.channel_id, source.ts, source.thread_ts))
    return footer(
        record.id,
        poster=credited.poster,
        others=list(credited.others),
        permalinks=links,
        update=update,
    )
