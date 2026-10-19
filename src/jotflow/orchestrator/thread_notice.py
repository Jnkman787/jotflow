"""Tell the thread where its idea went, once the issue is written (#32).

When a proposal is applied, JotFlow reacts on each message it was drawn from
and replies in its thread with the issue's link. The store is the outbox: an
applied proposal whose thread was not told is due, and one loop tells it. So
nothing in the agent's write path calls Slack.

Reactions come first. Adding one that is already there is harmless, so a
retry repeats them but never the reply, which is recorded as sent right after
it is posted. The reply is built by JotFlow alone, from the repo's nickname
and the issue URL that ``record_result`` checked against the approved repo.
Nothing in it is agent-written. With ``issue_links = "none"`` the reply still
carries the URL, since that is what a rebuild reads (#38).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from jotflow.models import ProposalRecord
from jotflow.policy import Policy
from jotflow.store.base import Store, utc_now

log = logging.getLogger("jotflow.thread")

BATCH = 50
FIRST_RETRY = timedelta(seconds=2)
LAST_RETRY = timedelta(minutes=1)

_ISSUE_URL = re.compile(
    r"https://github\.com/(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)/issues/(?P<number>[0-9]+)"
)
# Slack errors on a reaction that mean there is nothing to do for that message.
_REACTION_DONE = frozenset({"already_reacted", "message_not_found", "no_item_specified"})
# Slack errors on the reply that retrying cannot fix: the thread or channel is gone.
_REPLY_FINAL = frozenset(
    {"channel_not_found", "is_archived", "not_in_channel", "thread_not_found", "message_not_found"}
)


class ThreadSlack(Protocol):
    async def react(self, channel_id: str, ts: str, name: str) -> None: ...

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None: ...


def slack_error(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except Exception:
        return None
    return code if isinstance(code, str) else None


def reply_text(policy: Policy, record: ProposalRecord) -> str | None:
    """The thread reply, or None if the stored URL is not a GitHub issue URL."""
    url = record.result.issue_url if record.result else None
    match = _ISSUE_URL.fullmatch(url or "")
    if match is None:
        return None
    repo, number = match["repo"], match["number"]
    label = repo
    channel = policy.channels.get(record.channel_id)
    if channel is not None:
        for entry in channel.repos:
            if entry.repo.casefold() == repo.casefold():
                label = entry.nickname
    verb = "Updated" if record.proposal.kind == "update_issue" else "Created"
    return f"{verb} <{match.group(0)}|{label}#{number}>"


class ThreadNotice:
    """Reacts on an applied proposal's source messages and replies with its issue link."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        slack: ThreadSlack,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack
        self._clock = clock
        self._waiting: dict[str, tuple[datetime, timedelta]] = {}

    async def tell(self) -> int:
        """Tell every due thread that is not waiting to retry. Returns how many were told."""
        told = 0
        now = self._clock()
        for record in await self._store.threads_due(BATCH + len(self._waiting)):
            wait = self._waiting.get(record.id)
            if wait is not None and wait[0] > now:
                continue
            if await self._tell(record):
                told += 1
        return told

    async def _tell(self, record: ProposalRecord) -> bool:
        text = reply_text(self._policy, record)
        if text is None:
            log.warning(
                "proposal %s: applied without a GitHub issue URL; nothing to say", record.id
            )
            await self._store.mark_thread_told(record.id)
            return False
        try:
            await self._react_on_sources(record)
            await self._slack.reply(record.channel_id, record.thread_ts, text)
        except Exception as exc:
            code = slack_error(exc)
            if code in _REPLY_FINAL:
                log.warning("proposal %s: Slack said %s; the thread is not told", record.id, code)
                self._waiting.pop(record.id, None)
                await self._store.mark_thread_told(record.id)
                return False
            self._back_off(record.id)
            # The type only: an error's text could quote the thread.
            log.warning(
                "proposal %s: could not tell the thread (%s)", record.id, type(exc).__name__
            )
            return False
        await self._store.mark_thread_told(record.id)
        self._waiting.pop(record.id, None)
        log.info("proposal %s: thread told the issue link", record.id)
        return True

    async def _react_on_sources(self, record: ProposalRecord) -> None:
        name = self._policy.issue_reaction
        if not name:
            return
        seen: set[tuple[str, str]] = set()
        for ref in record.proposal.sources:
            if (ref.channel_id, ref.ts) in seen:
                continue
            seen.add((ref.channel_id, ref.ts))
            try:
                await self._slack.react(ref.channel_id, ref.ts, name)
            except Exception as exc:
                if slack_error(exc) not in _REACTION_DONE:
                    raise

    def _back_off(self, proposal_id: str) -> None:
        last = self._waiting.get(proposal_id)
        wait = FIRST_RETRY if last is None else min(last[1] * 2, LAST_RETRY)
        self._waiting[proposal_id] = (self._clock() + wait, wait)


class BoltThreadSlack:
    """:class:`ThreadSlack` over slack-sdk's client. Links never unfurl."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        await self._client.reactions_add(channel=channel_id, timestamp=ts, name=name)

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        await self._client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
