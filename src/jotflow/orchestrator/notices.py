"""What JotFlow says when it stops retrying an execution run (#30).

A proposal that reaches its limit of execution attempts is failed. If it has a
card, the card sync (#152) redraws it with the fixed failed line, since the
failure raised its revision. Otherwise one fixed reply goes to its thread.
Neither carries the agent's error, which can quote private repo content and
stays in the store.
"""

from __future__ import annotations

import logging

from jotflow.models import ProposalRecord
from jotflow.orchestrator.runs import MAX_EXECUTION_ATTEMPTS
from jotflow.policy import Policy
from jotflow.whiteboard.write_tools import ThreadPoster

log = logging.getLogger("jotflow.notices")

GAVE_UP_NOTICE = (
    "JotFlow stopped trying to write this proposal after {attempts} attempts. "
    "Nothing more will be written unless someone approves it again."
)


class GiveUpNotice:
    """Tells the thread that a proposal failed for good: on its card, or in one reply."""

    def __init__(
        self,
        poster: ThreadPoster,
        policy: Policy,
        *,
        attempts: int = MAX_EXECUTION_ATTEMPTS,
    ) -> None:
        self._poster = poster
        self._policy = policy
        self._attempts = attempts

    async def __call__(self, record: ProposalRecord) -> None:
        if record.card_ts:
            log.info("proposal %s: its card will show it failed", record.id)
            return
        await self._poster.post(
            record.channel_id, record.thread_ts, GAVE_UP_NOTICE.format(attempts=self._attempts)
        )
        log.info("proposal %s: thread told it failed", record.id)
