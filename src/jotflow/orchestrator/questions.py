"""Questions asked with ``ask:`` or an @-mention of the bot start a question run (#57, D12).

A question run is read-only: it answers once, in the thread, through the
``answer`` tool (:mod:`jotflow.whiteboard.write_tools`). This module decides
whether a message is a question, who may ask, and whether a run is queued.

- **What counts.** A person's new message that starts with ``ask:``, or that
  mentions the bot's own user id. A mention of anyone else, ``<!channel>``,
  ``<!here>`` and ``<!everyone>`` never do. Ingest has already dropped the
  bot's own messages, other bots, edits, and channels JotFlow doesn't watch,
  and the orchestrator does nothing in a paused channel.
- **Who may ask.** Only this workspace's own members. A question run reads the
  channel's repos with the team's own GitHub token, private ones included, and
  posts what it finds, so a guest, an external Slack Connect user, or a person
  whose lookup fails never starts one. They get one private, fixed reply.
- **How often.** Per channel per hour, per person, and one queued run per
  thread, from ``[questions]`` in jotflow.toml. Over a limit nothing is queued,
  and the person is told privately, at most once per ten minutes.
- **What the message becomes.** The agent's chatter, so drafting skips it. Not
  a person's: a person's chatter on a thread's first message would keep the
  whole thread out of drafting, as ``skip:`` does. The orchestrator also leaves
  a question out of the debouncer, so it costs no drafting run.

The asking message's text is read only to see the keyword or the mention. It is
never stored or logged.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from jotflow.models import MessageRef, RunKind
from jotflow.orchestrator.keywords import split_keyword
from jotflow.orchestrator.people import Person
from jotflow.policy import Policy, UnknownChannelError
from jotflow.reactions import CONFIRM
from jotflow.store.base import Store, utc_now

log = logging.getLogger("jotflow.questions")

_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")
# How long one person's private "not now" or "members only" reply keeps quiet.
NOTICE_QUIET_SECONDS = 600.0

MEMBERS_ONLY = "Questions are answered for this workspace's own members only."
TOO_MANY = "JotFlow is answering a lot of questions here right now. Try again in a few minutes."
TOO_SOON = "You asked JotFlow a question a moment ago. Try again in a minute."
OFF = "Questions aren't turned on in this channel."

Outcome = Literal["queued", "not_a_question", "refused", "limited", "off"]


class QuestionSlack(Protocol):
    """The Slack calls a question makes. Tests use a fake."""

    async def person(self, user_id: str) -> Person: ...

    async def own_team(self) -> str | None: ...

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None: ...

    async def react(self, channel_id: str, ts: str, name: str) -> None: ...


@dataclass(frozen=True)
class Asked:
    outcome: Outcome
    run_id: str | None = None


def mentions_bot(text: str, bot_user_id: str) -> bool:
    """Whether ``text`` mentions the bot's own user id, as Slack writes a mention."""
    if not _SLACK_USER.fullmatch(bot_user_id):
        return False
    return re.search(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", text) is not None


def is_question(text: str, bot_user_id: str) -> bool:
    keyword, _ = split_keyword(text)
    return keyword == "ask" or mentions_bot(text, bot_user_id)


class QuestionHandler:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        slack: QuestionSlack,
        bot_user_id: str,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack
        self._bot = bot_user_id
        self._clock = clock
        self._monotonic = monotonic
        # In memory: a restart forgets them, which only lets one question through sooner.
        self._asked_at: dict[tuple[str, str], float] = {}
        self._told_at: dict[tuple[str, str], float] = {}

    async def on_message(self, event: Mapping[str, Any]) -> Asked:
        """Queue a question run for this message if it is a question someone may ask."""
        text = event.get("text")
        channel = event.get("channel")
        ts = event.get("ts")
        user = event.get("user")
        if not (isinstance(text, str) and isinstance(channel, str) and isinstance(ts, str)):
            return Asked("not_a_question")
        if not is_question(text, self._bot):
            return Asked("not_a_question")
        thread_ts = event.get("thread_ts") if isinstance(event.get("thread_ts"), str) else None
        thread = thread_ts or ts
        if not isinstance(user, str) or not _SLACK_USER.fullmatch(user):
            return Asked("refused")
        try:
            settings = self._policy.questions_for(channel)
        except UnknownChannelError:
            return Asked("not_a_question")
        if not settings.enabled:
            await self._tell(channel, user, thread, OFF)
            return Asked("off")
        if not await self._may_ask(event, user):
            await self._tell(channel, user, thread, MEMBERS_ONLY)
            log.info("question in %s refused: the asker isn't a member of this workspace", channel)
            return Asked("refused")
        now = self._monotonic()
        last = self._asked_at.get((channel, user))
        if last is not None and now - last < settings.per_person_seconds:
            await self._tell(channel, user, thread, TOO_SOON)
            return Asked("limited")
        since = self._clock() - timedelta(hours=1)
        if await self._store.count_runs(channel, RunKind.QUESTION, since=since) >= (
            settings.per_hour
        ):
            await self._tell(channel, user, thread, TOO_MANY)
            log.info("question in %s not queued: the channel's hourly limit is reached", channel)
            return Asked("limited")
        # Not an idea, so drafting skips it. The agent's mark, not a person's, so a
        # question that starts a thread doesn't keep the thread's ideas out.
        await self._store.mark_chatter(
            MessageRef(channel_id=channel, ts=ts, thread_ts=thread_ts), by="agent"
        )
        # One queued question run per thread: enqueue_run returns the one already queued.
        run = await self._store.enqueue_run(RunKind.QUESTION, channel, thread)
        self._asked_at[(channel, user)] = now
        try:
            await self._slack.react(channel, ts, CONFIRM)
        except Exception:
            log.warning("could not acknowledge a question in %s", channel)
        log.info("question run %s queued in %s", run.id, channel)
        return Asked("queued", run.id)

    async def _may_ask(self, event: Mapping[str, Any], user: str) -> bool:
        """This workspace's own member, not a guest or external. Fails closed."""
        try:
            own = await self._slack.own_team()
            if own is None:
                return False
            # Slack Connect: a message from another workspace says so in the event.
            for key in ("user_team", "source_team"):
                team = event.get(key)
                if isinstance(team, str) and team and team != own:
                    return False
            person = await self._slack.person(user)
        except Exception:
            return False
        return not (person.guest or person.external)

    async def _tell(self, channel: str, user: str, thread: str, text: str) -> None:
        """One private, fixed reply, at most once per person per ten minutes."""
        now = self._monotonic()
        last = self._told_at.get((channel, user))
        if last is not None and now - last < NOTICE_QUIET_SECONDS:
            return
        self._told_at[(channel, user)] = now
        try:
            await self._slack.whisper(channel, user, thread, text)
        except Exception:
            log.warning("could not reply privately about a question in %s", channel)


class BoltQuestionSlack:
    """:class:`QuestionSlack` over slack-sdk: the same people lookup and private replies
    as keywords and help."""

    def __init__(self, client: Any, people: Any, whisperer: Any) -> None:
        self._client = client
        self._people = people
        self._whisperer = whisperer

    async def person(self, user_id: str) -> Person:
        result: Person = await self._people.person(user_id)
        return result

    async def own_team(self) -> str | None:
        team: str | None = await self._people.own_team()
        return team

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        await self._whisperer.whisper(channel_id, user_id, thread_ts, text)

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        await self._client.reactions_add(channel=channel_id, timestamp=ts, name=name)
