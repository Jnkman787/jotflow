"""Event ingest: decide which Slack events JotFlow acts on.

Pure and synchronous, so every rule is tested without Slack. The filter sees
raw event payloads, which include message text, but what it returns and what
it logs are pointers only: channel ids, timestamps, and reaction names.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from jotflow.models import MessageRef

log = logging.getLogger("jotflow.ingest")

# Message subtypes that are a person saying something new. Everything else,
# including edits, deletes, joins, and topic changes, is ignored.
NEW_MESSAGE_SUBTYPES = frozenset({None, "thread_broadcast", "file_share", "me_message"})


@dataclass(frozen=True)
class BotIdentity:
    """Who JotFlow is in this workspace, from ``auth.test``."""

    user_id: str
    bot_id: str | None = None


@dataclass(frozen=True)
class Accepted:
    kind: Literal["message", "reaction"]
    ref: MessageRef
    reaction: str | None = None


@dataclass(frozen=True)
class Ignored:
    reason: str
    channel_id: str | None = None


Decision = Accepted | Ignored


class _RecentKeys:
    """A bounded set that forgets the oldest keys first."""

    def __init__(self, capacity: int) -> None:
        self._keys: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._capacity = capacity

    def __contains__(self, key: tuple[str, ...]) -> bool:
        return key in self._keys

    def add(self, key: tuple[str, ...]) -> None:
        self._keys[key] = None
        self._keys.move_to_end(key)
        while len(self._keys) > self._capacity:
            self._keys.popitem(last=False)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class EventFilter:
    """Classifies events and drops the ones JotFlow must not act on.

    Ignored: events outside configured channels, the bot's own messages and
    reactions, edits, deletes, other message subtypes, and events Slack
    delivers more than once.
    """

    def __init__(
        self, channels: Collection[str], identity: BotIdentity, *, memory: int = 10_000
    ) -> None:
        self._channels = frozenset(channels)
        self._identity = identity
        self._seen = _RecentKeys(memory)

    def _is_own(self, event: Mapping[str, Any]) -> bool:
        if event.get("user") == self._identity.user_id:
            return True
        bot_id = self._identity.bot_id
        return bot_id is not None and event.get("bot_id") == bot_id

    def _deliver(
        self, decision: Accepted, keys: list[tuple[str, ...]], event_id: str | None
    ) -> Decision:
        if event_id:
            keys.append(("event", event_id))
        if any(key in self._seen for key in keys):
            return Ignored("duplicate delivery", decision.ref.channel_id)
        for key in keys:
            self._seen.add(key)
        return decision

    def classify(self, event: Mapping[str, Any], event_id: str | None = None) -> Decision:
        kind = event.get("type")
        if kind == "message":
            return self._message(event, event_id)
        if kind == "reaction_added":
            return self._reaction(event, event_id)
        return Ignored(f"event type {kind!r}")

    def _message(self, event: Mapping[str, Any], event_id: str | None) -> Decision:
        channel = _str(event.get("channel"))
        ts = _str(event.get("ts"))
        if channel is None or ts is None:
            return Ignored("message without channel or ts", channel)
        if channel not in self._channels:
            return Ignored("unconfigured channel", channel)
        subtype = event.get("subtype")
        if subtype == "message_changed":
            return Ignored("edit", channel)
        if subtype == "message_deleted":
            return Ignored("delete", channel)
        if subtype not in NEW_MESSAGE_SUBTYPES:
            return Ignored(f"subtype {subtype!r}", channel)
        if self._is_own(event):
            return Ignored("own message", channel)
        ref = MessageRef(channel_id=channel, ts=ts, thread_ts=_str(event.get("thread_ts")))
        return self._deliver(Accepted("message", ref), [("message", channel, ts)], event_id)

    def _reaction(self, event: Mapping[str, Any], event_id: str | None) -> Decision:
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "message":
            return Ignored("reaction on something other than a message")
        channel = _str(item.get("channel"))
        ts = _str(item.get("ts"))
        name = _str(event.get("reaction"))
        user = _str(event.get("user"))
        if channel is None or ts is None or name is None or user is None:
            return Ignored("reaction without channel, ts, name, or user", channel)
        if channel not in self._channels:
            return Ignored("unconfigured channel", channel)
        if self._is_own(event):
            return Ignored("own reaction", channel)
        ref = MessageRef(channel_id=channel, ts=ts)
        key = ("reaction", channel, ts, user, name)
        return self._deliver(Accepted("reaction", ref, reaction=name), [key], event_id)


def log_decision(decision: Decision) -> None:
    """One line per accepted event at INFO, ignored ones at DEBUG. Never any text."""
    if isinstance(decision, Accepted):
        ref = decision.ref
        if decision.kind == "message":
            log.info("message channel=%s ts=%s thread_ts=%s", ref.channel_id, ref.ts, ref.thread_ts)
        else:
            log.info("reaction channel=%s ts=%s name=%s", ref.channel_id, ref.ts, decision.reaction)
    else:
        log.debug("ignored: %s channel=%s", decision.reason, decision.channel_id)
