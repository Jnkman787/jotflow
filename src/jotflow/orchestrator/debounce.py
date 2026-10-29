"""The debouncer: queue a thread's drafting run once it has gone quiet (#19).

A thread is not handed to the agent until no new message has arrived in it for
its channel's window, ``debounce_seconds`` in jotflow.toml, which a channel may
override. Every message pushes the thread's due time back, so the first draft
reflects the whole burst.

Loose top-level messages each start a candidate topic (D9): a top-level post
is its own thread, keyed by its own ts (#104), with its own timer and its own
run. The agent attaches it to an existing topic, or starts one, from the topic
index and related excerpts. Posts are never grouped by time or author, which
would merge unrelated ideas.

Only accepted messages count. Ingest drops the bot's own messages, edits, and
redeliveries before they get here (#8), so nothing JotFlow posts can re-arm a
timer. Reactions do not start runs in v1.

A reply in a thread whose top-level message already belongs to a topic joins
that topic. Linking refreshes the topic's activity, so a topic that had aged
out of the index comes back for the run (D9). The agent can still move it.

The debouncer only queues runs, through the store, which coalesces a burst
that lands while the thread's run is still running into one queued run.
``RunLifecycle.start_next`` starts them (#107).

Due times live in memory and, as pointers and a time only, in the store
(#130). At startup :meth:`Debouncer.restore` re-arms every thread still
waiting, except one whose top-level message a person marked as chatter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from jotflow.models import MessageRef
from jotflow.orchestrator.ingest import Accepted, Decision
from jotflow.orchestrator.packet import FOLLOWS_WITHIN
from jotflow.policy import Policy
from jotflow.store.base import Clock, Store, utc_now

log = logging.getLogger("jotflow.debounce")


@dataclass(frozen=True)
class Thread:
    """A thread waiting out its quiet window."""

    channel_id: str
    thread_ts: str  # the parent's ts; a top-level post's own ts


def thread_of(ref: MessageRef) -> Thread:
    """The thread a message belongs to. A top-level post is its own thread."""
    return Thread(ref.channel_id, ref.thread_ts or ref.ts)


class Debouncer:
    def __init__(self, store: Store, policy: Policy, *, clock: Clock = utc_now) -> None:
        self._store = store
        self._policy = policy
        self._clock = clock
        self._due: dict[Thread, datetime] = {}
        # Each person's last top-level post in a channel, by its Slack ts (#334). In
        # memory: a restart only loses the ordering below, not the packet's hint.
        self._last_post: dict[tuple[str, str], tuple[float, Thread]] = {}

    def pending(self) -> dict[Thread, datetime]:
        """Threads waiting out their window, with when each falls due."""
        return dict(self._due)

    async def note(self, decision: Decision, author: str | None = None) -> Thread | None:
        """Take one ingest decision. Returns the thread it re-armed, if any.

        A top-level post that comes seconds after the same ``author``'s previous one
        waits until a window after that one's run is due, so the earlier post's
        topic exists for the later one to join (#334).
        """
        if not isinstance(decision, Accepted) or decision.kind != "message":
            return None
        ref = decision.ref
        if await self._skipped(ref):
            return None
        thread = thread_of(ref)
        window = timedelta(seconds=self._policy.debounce_for(ref.channel_id))
        due = self._clock() + window
        if author is not None and not (ref.thread_ts and ref.thread_ts != ref.ts):
            due = self._after_previous_post(ref, author, thread, due, window)
        # The durable copy first, so a failed save is never a thread that
        # waits in memory and then silently queues nothing (#130).
        await self._store.arm_thread(thread.channel_id, thread.thread_ts, due)
        self._due[thread] = due
        if ref.thread_ts and ref.thread_ts != ref.ts:
            await self._join_parents_topic(ref)
        return thread

    def _after_previous_post(
        self, ref: MessageRef, author: str, thread: Thread, due: datetime, window: timedelta
    ) -> datetime:
        try:
            posted = float(ref.ts)
        except ValueError:
            return due
        key = (ref.channel_id, author)
        previous = self._last_post.get(key)
        self._last_post[key] = (posted, thread)
        if previous is None or not 0 < posted - previous[0] <= FOLLOWS_WITHIN:
            return due
        earlier = self._due.get(previous[1])
        if earlier is None or previous[1] == thread:
            return due  # already drafted, or the same thread: nothing to wait for
        return max(due, earlier + window)

    async def arm(self, channel_id: str, thread_ts: str) -> Thread:
        """(Re)start a thread's quiet window for a message ingest never sees.

        JotFlow posts a person's Request Changes text itself (#27), and ingest
        drops JotFlow's own messages, so the gate arms the thread directly.
        """
        thread = Thread(channel_id, thread_ts)
        due = self._clock() + timedelta(seconds=self._policy.debounce_for(channel_id))
        await self._store.arm_thread(channel_id, thread_ts, due)
        self._due[thread] = due
        return thread

    async def _skipped(self, ref: MessageRef) -> bool:
        """A person said skip: for this message, or for the first message of its thread.

        Such a message never arms the thread, so ``skip:`` costs no agent run.
        The keyword handler runs first, so its mark is already stored (#56).
        """
        link = await self._store.get_message(ref.channel_id, ref.ts)
        if link is not None and link.chatter and link.linked_by == "person":
            return True
        if ref.thread_ts and ref.thread_ts != ref.ts:
            parent = await self._store.get_message(ref.channel_id, ref.thread_ts)
            return parent is not None and parent.chatter and parent.linked_by == "person"
        return False

    async def _join_parents_topic(self, reply: MessageRef) -> None:
        parent = await self._store.get_message(reply.channel_id, reply.thread_ts or "")
        if parent is None or parent.topic_id is None:
            return
        if await self._store.get_message(reply.channel_id, reply.ts) is not None:
            return  # already placed, by the agent or a person: leave that decision

        await self._store.link_message(reply, parent.topic_id, by="agent")

    async def restore(self) -> int:
        """At startup: wait out the windows the store kept. Returns how many.

        A thread whose top-level message a person marked as chatter, with
        ``skip:`` or the skip reaction (#56), is forgotten instead.
        """
        restored = 0
        for pending in await self._store.pending_threads():
            root = await self._store.get_message(pending.channel_id, pending.thread_ts)
            if root is not None and root.chatter and root.linked_by == "person":
                await self._store.drop_pending(pending.channel_id, pending.thread_ts)
                continue
            self._due[Thread(pending.channel_id, pending.thread_ts)] = pending.due_at
            restored += 1
        return restored

    def next_due(self) -> datetime | None:
        """When the next thread falls due, so a loop can sleep exactly that long."""
        return min(self._due.values(), default=None)

    async def flush(self, now: datetime | None = None) -> list[Thread]:
        """Queue a drafting run for every thread whose window has passed.

        Returns the threads queued, leaving out any a person skipped meanwhile.
        """
        moment = now or self._clock()
        ready = sorted(
            (t for t, due in self._due.items() if due <= moment),
            key=lambda t: (self._due[t], t.channel_id, t.thread_ts),
        )
        queued: list[Thread] = []
        for thread in ready:
            # Queues the run and forgets the thread in one step, so a crash
            # between the two cannot queue it twice after a restart. The store
            # queues nothing if a person skipped the thread's first message
            # after it was armed, such as by a skip reaction in the window.
            run = await self._store.queue_pending(thread.channel_id, thread.thread_ts)
            del self._due[thread]
            if run is None:
                log.info(
                    "thread %s in %s was skipped while it waited; no run",
                    thread.thread_ts,
                    thread.channel_id,
                )
                continue
            queued.append(thread)
            log.info(
                "thread %s in %s is quiet; drafting run queued", thread.thread_ts, thread.channel_id
            )
        return queued
