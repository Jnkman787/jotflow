"""A weekly summary of each channel's ideas (#308).

A channel with ``[channels.<id>.summary]`` gets one top-level post a week, at its
day, time and timezone, listing the week's ideas. An idea is a topic with a
proposal to create its issue made in the seven days before the post. Each item
shows its title, who suggested it, who added to it, the day, its status and
links to its thread, its card while it waits, and its issue once filed. Items
still waiting get **Not this week**, which puts the topic off to the next summary.

Rules (security, #315):

- **Nothing removed comes back.** An idea whose draft was cleared, that was
  rejected or superseded, or whose source messages were all deleted, is left out.
- **Plain text only.** Titles and names are ``plain_text``, so nothing in them is
  markup or a mention. Every link is a button JotFlow built.
- **Bounded.** At most :data:`MAX_ITEMS` items, waiting ones first, each title cut
  to the card's title limit, then "and N more".
- **Paused and gone channels are skipped**, checked when posting. Nothing is posted
  anywhere to complain.
- **Once a week.** The store holds a row per channel and slot, claimed before
  posting, so a restart never posts twice. A slot missed by more than
  :data:`LATE_LIMIT` is skipped rather than posted days late.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from jotflow.execution.attribution import Attribution, credit
from jotflow.execution.writes import _credited, permalink
from jotflow.models import ChannelMode, IssueProposal, ProposalRecord, ProposalState
from jotflow.policy import ChannelSummary, Policy
from jotflow.store.base import Store, utc_now
from jotflow.store.titles import MAX_TITLE_LENGTH, clean_title

log = logging.getLogger("jotflow.summary")

WINDOW = timedelta(days=7)
LATE_LIMIT = timedelta(hours=12)
MAX_ITEMS = 20
TICK_SECONDS = 60.0
ACTION_NOT_THIS_WEEK = "jotflow_summary_defer"
ACTION_LINK = "jotflow_summary_link"
# Slack errors that mean the bot can't post in the channel: skip the week quietly.
GONE = frozenset({"not_in_channel", "channel_not_found", "is_archived", "missing_scope"})

_STATUS = {
    ProposalState.PENDING: "Waiting for approval",
    ProposalState.APPROVED: "Being filed",
    ProposalState.EXECUTING: "Being filed",
    ProposalState.RECONCILING: "Being filed",
    ProposalState.APPLIED: "Filed",
    ProposalState.FAILED: "Couldn't be filed",
}
WAITING = frozenset({ProposalState.PENDING})
_ISSUE = re.compile(
    r"https://github\.com/(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)/issues/(?P<number>[0-9]+)"
)


def latest_slot(now: datetime, setting: ChannelSummary) -> datetime:
    """The most recent summary time at or before ``now``, in UTC.

    The day and time are the channel's own, in its timezone. A time that doesn't
    exist that day, in a spring-forward gap, comes an hour later; one that happens
    twice, in an autumn fall-back, is the first.
    """
    zone = ZoneInfo(setting.timezone)
    local = now.astimezone(zone)
    back = (local.weekday() - setting.weekday) % 7
    day = local.date() - timedelta(days=back)
    slot = _at(day, setting, zone)
    if slot > now:
        slot = _at(day - timedelta(days=7), setting, zone)
    return slot


def next_slot(slot: datetime, setting: ChannelSummary) -> datetime:
    """The summary after ``slot``: the same local day and time, a week on."""
    zone = ZoneInfo(setting.timezone)
    return _at(slot.astimezone(zone).date() + timedelta(days=7), setting, zone)


def _at(day: date, setting: ChannelSummary, zone: ZoneInfo) -> datetime:
    hour, minute = setting.hour_minute
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)


@dataclass(frozen=True)
class Item:
    """One idea in a summary. Every text field is shown as plain text."""

    topic_id: str
    title: str
    status: str
    waiting: bool
    day: str
    suggested_by: str | None
    added_by: tuple[str, ...]
    thread_url: str | None
    card_ts: str | None
    issue_url: str | None
    from_last_week: bool


class SummarySlack(Protocol):
    async def post(self, channel_id: str, text: str, blocks: list[dict[str, Any]]) -> str: ...

    async def update(
        self, channel_id: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> None: ...

    async def permalink(self, channel_id: str, ts: str) -> str | None: ...


async def collect(
    store: Store,
    channel_id: str,
    slot: datetime,
    setting: ChannelSummary,
    attribution: Attribution,
    nicknames_for: Mapping[str, str] | None = None,
) -> list[Item]:
    """The week's ideas for ``channel_id``, newest first, never one that was removed."""
    since = slot - WINDOW
    zone = ZoneInfo(setting.timezone)
    deferred = {d.topic_id for d in await store.deferred_topics(channel_id, slot)}
    topics = {t.topic.id: t for t in await store.channel_topics(channel_id, active_since=since)}
    for topic_id in deferred - set(topics):
        found = await store.get_topic(topic_id)
        if found is not None and found.topic.channel_id == channel_id:
            topics[topic_id] = found
    workspace = await _workspace(attribution)
    nicknames = dict(nicknames_for or {})
    items: list[tuple[datetime, Item]] = []
    for topic_id, found in topics.items():
        record = await _newest_create(store, channel_id, topic_id, found.topic.messages)
        if record is None:
            continue
        carried = topic_id in deferred
        if not carried and not since <= record.created_at < slot:
            continue
        if not await _shown(record, attribution):
            continue
        draft = record.proposal
        assert isinstance(draft, IssueProposal)
        try:
            if record.draft_cleared_at is not None:  # filed: the draft text is gone
                raise ValueError("no draft")
            title = clean_title(draft.title, MAX_TITLE_LENGTH)
        except ValueError:
            title = clean_title(found.topic.title, MAX_TITLE_LENGTH)
        try:
            who = await credit(record, attribution, store)
        except Exception as exc:  # names are a nicety: the item still shows
            log.warning("summary %s: names unknown (%s)", record.id, type(exc).__name__)
            who = None
        status = _STATUS.get(record.state, "Waiting for approval")
        issue_url = record.result.issue_url if record.result else None
        if record.state is ProposalState.APPLIED and issue_url:
            filed = _ISSUE.fullmatch(issue_url)
            if filed is not None:
                repo = nicknames.get(filed["repo"].casefold(), filed["repo"])
                status = f"Filed as {repo}#{filed['number']}"
        items.append(
            (
                record.created_at,
                Item(
                    topic_id=topic_id,
                    title=title,
                    status=status,
                    waiting=record.state in WAITING,
                    day=record.created_at.astimezone(zone).strftime("%a %d %b"),
                    suggested_by=_one_name(who.poster if who else None),
                    added_by=tuple(_credited(who.others, skip=_one_name(who.poster) or ""))
                    if who
                    else (),
                    thread_url=permalink(workspace, channel_id, record.thread_ts)
                    if workspace
                    else None,
                    card_ts=record.card_ts if record.state in WAITING else None,
                    issue_url=issue_url if record.state is ProposalState.APPLIED else None,
                    from_last_week=carried,
                ),
            )
        )
    ordered = sorted(items, key=lambda pair: pair[0], reverse=True)
    return [item for _, item in sorted(ordered, key=lambda pair: not pair[1].waiting)]


def _one_name(name: str | None) -> str | None:
    named = _credited([name]) if name else []
    return named[0] if named else None


async def _workspace(attribution: Attribution) -> str | None:
    try:
        return await attribution.workspace_url()
    except Exception:
        return None


async def _newest_create(
    store: Store, channel_id: str, topic_id: str, messages: Sequence[Any]
) -> ProposalRecord | None:
    threads = {ref.thread_ts or ref.ts for ref in messages if ref.channel_id == channel_id}
    found: list[ProposalRecord] = []
    for thread_ts in sorted(threads):
        for record in await store.thread_proposals(channel_id, thread_ts):
            if isinstance(record.proposal, IssueProposal) and record.proposal.topic_id == topic_id:
                found.append(record)
    return max(found, key=lambda r: r.created_at) if found else None


async def _shown(record: ProposalRecord, attribution: Attribution) -> bool:
    """Whether the idea may be posted again: not removed by anyone (security, #315)."""
    # Filing clears a draft's text by design; a filed idea still shows, by its topic's
    # title and its issue. Any other cleared draft was removed on purpose.
    if record.draft_cleared_at is not None and record.state is not ProposalState.APPLIED:
        return False
    if record.state in (ProposalState.REJECTED, ProposalState.SUPERSEDED):
        return False
    sources = record.proposal.sources
    if not sources:
        return True
    for source in sources:
        try:
            await attribution.authors([source])
            return True  # at least one source is still there
        except LookupError:
            continue
        except Exception:
            return True  # Slack unsure: the card is still in the channel anyway
    return False  # every source deleted


def blocks_for(
    items: Sequence[Item],
    *,
    since: datetime,
    slot: datetime,
    setting: ChannelSummary,
    card_links: Mapping[str, str],
    late: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """The summary's text and blocks, within Slack's limits."""
    zone = ZoneInfo(setting.timezone)
    span = (
        f"{since.astimezone(zone).strftime('%d %b')} to {slot.astimezone(zone).strftime('%d %b')}"
    )
    shown = list(items[:MAX_ITEMS])
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain("This week's ideas")},
        {"type": "context", "elements": [_plain(span + (", posted late" if late else ""))]},
    ]
    if not shown:
        blocks.append({"type": "section", "text": _plain("No new ideas this week.")})
    for item in shown:
        lines = [item.title, f"{item.status}, {item.day}"]
        if item.suggested_by:
            lines.append(f"Suggested by {item.suggested_by}")
        if item.added_by:
            lines.append("Added to by " + ", ".join(item.added_by))
        if item.from_last_week:
            lines.append("Put off from last week")
        text = _plain("\n".join(lines))
        blocks.append({"type": "section", "block_id": f"item:{item.topic_id}", "text": text})
        buttons: list[dict[str, Any]] = []
        if item.thread_url:
            buttons.append(_link("Thread", item.thread_url))
        if item.issue_url:
            buttons.append(_link("Issue", item.issue_url))
        if item.waiting and item.topic_id in card_links:
            buttons.append(_link("Open card", card_links[item.topic_id]))
        if item.waiting:
            buttons.append(
                {
                    "type": "button",
                    "text": _plain("Not this week"),
                    "action_id": ACTION_NOT_THIS_WEEK,
                    "value": f"{item.topic_id}|{slot.isoformat()}",
                }
            )
        if buttons:
            blocks.append(
                {"type": "actions", "block_id": f"actions:{item.topic_id}", "elements": buttons}
            )
    waiting = sum(1 for item in items if item.waiting)
    footer = f"{len(items)} idea{'s' if len(items) != 1 else ''}, {waiting} waiting for approval"
    if len(items) > len(shown):
        footer += f". And {len(items) - len(shown)} more: see the channel"
    blocks.append({"type": "context", "elements": [_plain(footer)]})
    return f"This week's ideas: {len(items)}", blocks


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text[:3000], "emoji": False}


def _link(label: str, url: str) -> dict[str, Any]:
    return {"type": "button", "text": _plain(label), "url": url, "action_id": ACTION_LINK}


class WeeklySummaries:
    """Posts each channel's summary when it's due, and answers Not this week."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        slack: SummarySlack,
        attribution: Attribution,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack
        self._attribution = attribution
        self._clock = clock

    async def tick(self) -> list[str]:
        """Post every summary that's due now. Returns the channels posted to."""
        posted: list[str] = []
        now = self._clock()
        for channel_id, channel in self._policy.channels.items():
            setting = channel.summary
            if setting is None:
                continue
            slot = latest_slot(now, setting)
            if await self._post(channel_id, setting, slot, now):
                posted.append(channel_id)
        return posted

    async def _post(
        self, channel_id: str, setting: ChannelSummary, slot: datetime, now: datetime
    ) -> bool:
        existing = await self._store.get_summary(channel_id, slot)
        if existing is not None and existing.status in ("posted", "skipped"):
            return False
        if not await self._store.start_summary(channel_id, slot):
            return False
        if now - slot > LATE_LIMIT:
            log.info("summary for %s at %s missed by too long; skipped", channel_id, slot)
            await self._store.finish_summary(channel_id, slot, "skipped")
            return False
        mode = await self._store.get_channel_mode(channel_id)
        if mode is not None and mode.mode is ChannelMode.PAUSED:
            log.info("summary for %s skipped: the channel is paused", channel_id)
            await self._store.finish_summary(channel_id, slot, "skipped")
            return False
        try:
            nicknames = {
                r.repo.casefold(): r.nickname
                for r in self._policy.channels[channel_id].repos
                if r.nickname
            }
            items = await collect(
                self._store, channel_id, slot, setting, self._attribution, nicknames
            )
            links: dict[str, str] = {}
            for item in items[:MAX_ITEMS]:
                if item.waiting and item.card_ts:
                    url = await self._slack.permalink(channel_id, item.card_ts)
                    if url:
                        links[item.topic_id] = url
            text, blocks = blocks_for(
                items,
                since=slot - WINDOW,
                slot=slot,
                setting=setting,
                card_links=links,
                late=now - slot > timedelta(minutes=5),
            )
            ts = await self._slack.post(channel_id, text, blocks)
        except Exception as exc:
            error = slack_error(exc)
            if error in GONE:
                log.info("summary for %s skipped: %s", channel_id, error)
                await self._store.finish_summary(channel_id, slot, "skipped")
            else:
                log.warning("summary for %s failed (%s)", channel_id, error or type(exc).__name__)
                await self._store.finish_summary(channel_id, slot, "failed")
            return False
        await self._store.finish_summary(channel_id, slot, "posted", ts)
        log.info("summary for %s posted with %d idea(s)", channel_id, len(items))
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Check every minute until ``stop`` is set."""
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("the weekly summary loop failed; trying again in a minute")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), TICK_SECONDS)

    async def defer(self, user_id: str, channel_id: str, message_ts: str, value: str) -> str:
        """Put a topic off to the next summary, after the gate checked who clicked.

        The click must come from this week's own summary message, for a topic it
        lists. Returns the fixed reply for the person.
        """
        topic_id, _, raw_slot = value.partition("|")
        setting = self._policy.channels.get(channel_id)
        summary = setting.summary if setting is not None else None
        try:
            slot = datetime.fromisoformat(raw_slot)
        except ValueError:
            return NOT_THIS_SUMMARY
        if summary is None or slot.tzinfo is None:
            return NOT_THIS_SUMMARY
        post = await self._store.get_summary(channel_id, slot.astimezone(UTC))
        if post is None or post.ts != message_ts or post.status != "posted":
            return NOT_THIS_SUMMARY
        topic = await self._store.get_topic(topic_id)
        if topic is None or topic.topic.channel_id != channel_id:
            return NOT_THIS_SUMMARY
        until = next_slot(post.slot, summary)
        await self._store.defer_topic(topic_id, channel_id, until, by=user_id)
        log.info("topic %s put off to the next summary by user %s", topic_id, user_id)
        return DEFERRED


NOT_THIS_SUMMARY = "That summary has passed, so this button no longer works."
DEFERRED = "Put off to next week's summary."


def deferred_blocks(
    blocks: Sequence[Mapping[str, Any]], topic_id: str, name: str
) -> list[dict[str, Any]]:
    """The summary's blocks with one item's buttons replaced by who put it off."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("block_id") == f"actions:{topic_id}":
            out.append(
                {
                    "type": "context",
                    "block_id": f"actions:{topic_id}",
                    "elements": [_plain(f"Put off to next week by {name}")],
                }
            )
        else:
            out.append(dict(block))
    return out


def slack_error(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    data = getattr(response, "data", None)
    if isinstance(data, Mapping) and isinstance(data.get("error"), str):
        return str(data["error"])
    return None


class BoltSummarySlack:
    """The summary's Slack calls: post and edit in the channel, and a card's link."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def post(self, channel_id: str, text: str, blocks: list[dict[str, Any]]) -> str:
        response = await self._client.chat_postMessage(
            channel=channel_id, text=text, blocks=blocks, unfurl_links=False, unfurl_media=False
        )
        return str(response["ts"])

    async def update(
        self, channel_id: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> None:
        await self._client.chat_update(channel=channel_id, ts=ts, text=text, blocks=blocks)

    async def permalink(self, channel_id: str, ts: str) -> str | None:
        try:
            response = await self._client.chat_getPermalink(channel=channel_id, message_ts=ts)
        except Exception:
            return None
        link = response.get("permalink")
        return link if isinstance(link, str) and link.startswith("https://") else None


class Names(Protocol):
    async def name(self, user_id: str) -> str | None: ...


def register_summary(
    app: Any, summaries: WeeklySummaries, gate: Any, slack: SummarySlack, names: Names
) -> None:
    """Answer the summary's buttons. Each acks first, within Slack's three seconds.

    A link button needs nothing but the ack. Not this week goes through the
    approval gate's ``checked_click``: Approve's rules for who may click.
    """

    async def link(ack: Any) -> None:
        await ack()

    async def not_this_week(ack: Any, body: dict[str, Any]) -> None:
        await ack()

        async def act(click: Any) -> str:
            reply = await summaries.defer(
                click.user_id, click.channel_id, click.message_ts, click.value
            )
            if reply == DEFERRED:
                topic_id = click.value.partition("|")[0]
                raw = body.get("message")
                message: dict[str, Any] = raw if isinstance(raw, dict) else {}
                listed = message.get("blocks")
                old: list[Mapping[str, Any]] = (
                    [b for b in listed if isinstance(b, dict)] if isinstance(listed, list) else []
                )
                who = await names.name(click.user_id) or "someone"
                blocks = deferred_blocks(old, topic_id, _one_name(who) or "someone")
                try:
                    await slack.update(
                        click.channel_id, click.message_ts, str(message.get("text", "")), blocks
                    )
                except Exception as exc:
                    log.warning("summary edit failed (%s)", type(exc).__name__)
            await gate.tell(click.user_id, reply, channel=click.channel_id)
            return "deferred" if reply == DEFERRED else "refused"

        try:
            await gate.checked_click(body, act)
        except Exception:
            log.exception("Not this week failed")

    app.action(ACTION_LINK)(link)
    app.action(ACTION_NOT_THIS_WEEK)(not_this_week)
