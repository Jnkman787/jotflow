"""Apply ``re:`` and ``skip:`` without an agent run, and confirm keywords (#56, D10).

``idea:`` and ``bug:`` are hints the work packet hands the agent; JotFlow only
confirms them. ``re:`` and ``skip:`` JotFlow applies itself, as the poster's
own decision, so they are stored as a person's (``by="person"``):

- ``skip:`` marks the message as chatter. On a top-level message the packet
  already leaves the whole thread out.
- ``re: web#42`` links the message to the topic for that issue, creating one
  if the issue has none yet. A reference outside the channel's repos is
  refused with a reply that says why (D11).

The ``no_entry_sign`` reaction skips a message. Reactions from guests and
external users are ignored. With approvers off anyone may skip someone else's
message, and JotFlow says so in the thread; with approvers on only the author
or an approver may (D7, D10).

**Order with the debouncer.** :meth:`jotflow.orchestrator.service.Orchestrator.note`
calls :meth:`KeywordHandler.on_message` before
:meth:`~jotflow.orchestrator.debounce.Debouncer.note`. The debouncer joins a
reply to its parent's topic only when the reply is not placed yet, so a
person's ``re:`` or ``skip:`` stored first is left alone. A skip reaction that
arrives later overrides the debouncer's agent-level link, as a person's
decision beats the agent's.

Nothing here approves, rejects, or changes a channel's mode: no keyword can
skip approval or change the mode.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from jotflow.models import MessageRef
from jotflow.orchestrator.keywords import (
    REACTIONS,
    ReferenceError,
    parse_reference,
    split_keyword,
)
from jotflow.orchestrator.people import Person as Person  # re-exported: callers import it here
from jotflow.orchestrator.people import SlackPeople
from jotflow.policy import Policy
from jotflow.reactions import CONFIRM as REACTION_CONFIRM
from jotflow.store.base import Store

log = logging.getLogger("jotflow.keywords")

CONFIRM = REACTION_CONFIRM
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")

Action = Literal["confirmed", "skipped", "attached", "refused", "ignored"]


@dataclass(frozen=True)
class Outcome:
    action: Action
    detail: str = ""


class KeywordSlack(Protocol):
    """The Slack calls keyword handling makes. Tests use a fake."""

    async def react(self, channel_id: str, ts: str, name: str) -> None: ...

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None: ...

    async def person(self, user_id: str) -> Person: ...


def _mention(user_id: str | None) -> str:
    return f"<@{user_id}>" if user_id and _SLACK_USER.fullmatch(user_id) else "Someone"


class KeywordHandler:
    def __init__(self, store: Store, policy: Policy, slack: KeywordSlack) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack

    async def on_message(self, event: Mapping[str, Any]) -> Outcome:
        """Handle an accepted message that may start with a keyword."""
        channel = str(event.get("channel") or "")
        ts = str(event.get("ts") or "")
        thread_ts = event.get("thread_ts") if isinstance(event.get("thread_ts"), str) else None
        keyword, rest = split_keyword(str(event.get("text") or ""))
        if keyword is None or not channel or not ts:
            return Outcome("ignored")
        ref = MessageRef(channel_id=channel, ts=ts, thread_ts=thread_ts)
        if keyword == "skip":
            await self._store.mark_chatter(ref, by="person")
            outcome = Outcome("skipped")
        elif keyword == "re":
            outcome = await self._attach(ref, rest)
            if outcome.action == "refused":
                return outcome
        else:
            outcome = Outcome("confirmed", keyword)
        await self._slack.react(channel, ts, CONFIRM)
        log.info("keyword %s on %s in %s: %s", keyword, ts, channel, outcome.action)
        return outcome

    async def _attach(self, ref: MessageRef, rest: str) -> Outcome:
        try:
            reference = parse_reference(rest, self._policy.channel(ref.channel_id))
        except ReferenceError as exc:
            await self._slack.reply(
                ref.channel_id,
                ref.thread_ts or ref.ts,
                f"I could not attach this message: {exc}.",
            )
            return Outcome("refused", str(exc))
        topic_id = await self._topic_for(ref.channel_id, reference.repo.repo, reference.number)
        if topic_id is None:
            created = await self._store.create_topic(ref.channel_id, reference.handle)
            topic_id = created.topic.id
            await self._store.set_topic_issue(topic_id, reference.repo.repo, reference.number)
        await self._store.link_message(ref, topic_id, by="person")
        return Outcome("attached", reference.handle)

    async def _topic_for(self, channel_id: str, repo: str, number: int) -> str | None:
        for record in await self._store.channel_topics(channel_id):
            topic = record.topic
            if (
                topic.repo
                and topic.repo.casefold() == repo.casefold()
                and (topic.issue_number == number)
            ):
                return topic.id
        return None

    async def on_reaction(self, event: Mapping[str, Any]) -> Outcome:
        """Handle an accepted reaction that may stand for a keyword."""
        keyword = REACTIONS.get(str(event.get("reaction") or ""))
        raw_item = event.get("item")
        item: Mapping[str, Any] = raw_item if isinstance(raw_item, Mapping) else {}
        channel = str(item.get("channel") or "")
        ts = str(item.get("ts") or "")
        user = str(event.get("user") or "")
        if keyword is None or not channel or not ts or not user:
            return Outcome("ignored")
        person = await self._slack.person(user)
        if person.guest or person.external:
            return Outcome("ignored", "guest or external user")
        author = event.get("item_user") if isinstance(event.get("item_user"), str) else None
        approvers = self._policy.approval.approvers
        # Without a known author, assume someone else's message: the approver rule holds.
        someone_else = author != user
        if someone_else and approvers and user not in approvers:
            return Outcome("ignored", "only the author or an approver may")
        stored = await self._store.get_message(channel, ts)
        ref = MessageRef(
            channel_id=channel, ts=ts, thread_ts=stored.ref.thread_ts if stored else None
        )
        await self._store.mark_chatter(ref, by="person")
        await self._slack.react(channel, ts, CONFIRM)
        if someone_else:
            await self._slack.reply(
                channel,
                ref.thread_ts or ts,
                f"{_mention(user)} marked {_mention(author)}'s message as not an idea, so it "
                "stays out of drafting. If that is wrong, say so in this thread.",
            )
        return Outcome("skipped")


class BoltKeywordSlack:
    """:class:`KeywordSlack` over slack-sdk's client."""

    def __init__(
        self, client: Any, team_id: str | None = None, *, people: SlackPeople | None = None
    ) -> None:
        self._client = client
        self._people = people or SlackPeople(client, team_id)

    async def _own_team(self) -> str | None:
        """This workspace's id, from ``auth.test`` once, unless it was given."""
        return await self._people.own_team()

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        await self._client.reactions_add(channel=channel_id, timestamp=ts, name=name)

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        await self._client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=text, unfurl_links=False
        )

    async def person(self, user_id: str) -> Person:
        return await self._people.person(user_id)
