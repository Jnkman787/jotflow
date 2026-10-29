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

import logging
from collections.abc import Sequence
from typing import Protocol

import anyio

from jotflow.execution.writes import footer, permalink
from jotflow.models import MessageRef, ProposalRecord
from jotflow.policy import Policy
from jotflow.store.base import Store

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


async def _contributed(record: ProposalRecord, store: Store | None) -> list[MessageRef]:
    """The messages a new issue credits: its sources, then the other messages linked to
    its topic in the same thread, in time order. Never another thread's."""
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
    extra: list[MessageRef] = []
    for topic_id in sorted(topics):
        found = await store.get_topic(topic_id)
        if found is None or found.topic.channel_id != record.channel_id:
            continue
        for ref in found.topic.messages:
            in_thread = (ref.thread_ts or ref.ts) == record.thread_ts
            if in_thread and ref.channel_id == record.channel_id and ref.ts not in seen:
                seen.add(ref.ts)
                extra.append(ref)
    return refs + sorted(extra, key=lambda ref: float(ref.ts))


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
    poster: str | None = None
    others: list[str] = []
    links: list[str] = []
    base: str | None = None
    if attribution is not None:
        with anyio.move_on_after(ATTRIBUTION_SECONDS):
            try:
                poster_id: str | None = None
                if not update:
                    root = MessageRef(channel_id=record.channel_id, ts=record.thread_ts)
                    authors = await attribution.authors([root])
                    poster_id = next(iter(authors), None)  # a bot's root has no person
                    if poster_id is not None:
                        poster = await attribution.name(poster_id)
                # Only people Slack confirms wrote a credited message, never the
                # agent-written participants list (#156 review). An update credits its
                # own sources: who added the new information (#307).
                refs = (
                    list(record.proposal.sources) if update else await _contributed(record, store)
                )
                for user in await _authors_of(attribution, refs):
                    if user != poster_id and (name := await attribution.name(user)):
                        others.append(name)
                base = await attribution.workspace_url()
            except Exception as exc:  # a lookup failure leaves that part out
                log.warning(
                    "proposal %s: attribution incomplete (%s)", record.id, type(exc).__name__
                )
    for source in record.proposal.sources:
        if source.permalink:
            links.append(source.permalink)
        elif base:
            links.append(permalink(base, source.channel_id, source.ts, source.thread_ts))
    return footer(record.id, poster=poster, others=others, permalinks=links, update=update)
