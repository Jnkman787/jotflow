"""Who raised an approved proposal, and where in Slack, for its issue footer (#31).

The execution packet never carries thread text. The footer needs only the
thread root's author (as a user id, from the same lookup approval uses), the
participants' display names, and the workspace URL to build permalinks. Each
lookup may fail or be slow; then that part of the footer is left out, and the
write still goes ahead.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

import anyio

from jotflow.execution.writes import footer, permalink
from jotflow.models import MessageRef, ProposalRecord
from jotflow.policy import Policy

log = logging.getLogger("jotflow.execution")

# The whole people phase of an execution get_work, inside the 30 s request timeout.
ATTRIBUTION_SECONDS = 10.0


class Attribution(Protocol):
    async def authors(self, refs: Sequence[MessageRef]) -> set[str]: ...

    async def name(self, user_id: str) -> str | None: ...

    async def workspace_url(self) -> str | None: ...


async def execution_footer(
    record: ProposalRecord, policy: Policy, attribution: Attribution | None
) -> str:
    """The footer for ``record``'s writes. Never raises for a failed Slack lookup."""
    if policy.issue_links == "none":
        return footer(record.id, poster=None, issue_links="none")
    poster: str | None = None
    others: list[str] = []
    links: list[str] = []
    base: str | None = None
    if attribution is not None:
        with anyio.move_on_after(ATTRIBUTION_SECONDS):
            try:
                root = MessageRef(channel_id=record.channel_id, ts=record.thread_ts)
                authors = await attribution.authors([root])
                poster_id = next(iter(authors), None)  # a bot's root has no person
                if poster_id is not None:
                    poster = await attribution.name(poster_id)
                for user in record.participants:
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
    return footer(record.id, poster=poster, others=others, permalinks=links)
