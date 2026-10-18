"""The topic index: a channel's active topics, compact enough for every prompt (#12).

A topic is active while a message was linked to it within the channel's window,
``topic_window_days`` in jotflow.toml. Older topics stay in the store, so a late
reply can still find its issue; they are just not shown to the agent.

Titles were written by earlier agent runs, so they are untrusted data. The
store cleans them on every write; the index cleans them again on the way out,
and :class:`~jotflow.models.TopicIndex` carries a note saying how to treat them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jotflow.models import TopicIndex, TopicSummary
from jotflow.policy import Policy
from jotflow.store.base import Store, TopicRecord, utc_now
from jotflow.store.titles import clean_title

DEFAULT_LIMIT = 50
MAX_THREADS = 5


def _summary(record: TopicRecord) -> TopicSummary:
    topic = record.topic
    threads: list[str] = []
    for ref in sorted(topic.messages, key=lambda m: m.ts, reverse=True):
        thread = ref.thread_ts or ref.ts
        if thread not in threads:
            threads.append(thread)
    try:
        title = clean_title(topic.title)
    except ValueError:  # pragma: no cover - the store never stores an empty title
        title = "(untitled)"
    return TopicSummary(
        id=topic.id,
        title=title,
        repo=topic.repo,
        issue_number=topic.issue_number,
        message_count=len(topic.messages),
        threads=threads[:MAX_THREADS],
        last_active=record.last_activity_at,
    )


async def build_index(
    store: Store,
    channel_id: str,
    *,
    window_days: int,
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TopicIndex:
    """The channel's topics active within ``window_days``, most recently active first."""
    if window_days < 1:
        raise ValueError("the topic window is at least one day")
    if limit < 1:
        raise ValueError("the index shows at least one topic")
    moment = now or utc_now()
    records = await store.channel_topics(
        channel_id, active_since=moment - timedelta(days=window_days)
    )
    return TopicIndex(
        channel_id=channel_id,
        window_days=window_days,
        generated_at=moment,
        topics=[_summary(r) for r in records[:limit]],
        more=max(0, len(records) - limit),
    )


async def list_topics(
    store: Store,
    policy: Policy,
    channel_id: str,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TopicIndex:
    """The index for a configured channel, with the window from jotflow.toml.

    Raises :class:`~jotflow.policy.UnknownChannelError` for a channel the policy
    does not list, so the agent can never read another channel's topics.
    """
    policy.channel(channel_id)
    return await build_index(
        store, channel_id, window_days=policy.topic_window_days, now=now, limit=limit
    )
