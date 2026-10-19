# Keep quiet-window timers across a restart

## Links and status

- Issue: #130
- Also serves: #19, the debouncer this extends
- Draft PR: #145
- Status: ready for review
- Last verified: `make check` green, 1598 tests, 98.8% coverage, on 2026-10-18
- Next safe action: reviewer reviews #145

## Objective and done-when

The debouncer (#19) keeps each thread's due time in memory. A thread waiting out its quiet window when JotFlow stops gets no drafting run until someone posts again, because Slack does not redeliver messages after a restart. This issue keeps those due times in the store and re-arms them at startup.

The issue's boxes, refined:

- [x] A thread pending at shutdown is queued for its drafting run after a restart, once its due time has passed. A thread whose due time passed while JotFlow was down is queued on the first flush.
- [x] Pending entries hold no message text: only the channel id, the thread ts, and the due time. The schema test covers the new table.
- [x] A thread flushed or run before the restart is not queued twice. Flushing queues the drafting run and deletes the pending row in one store transaction, so a crash can't leave both.

From the lead:

- [x] Startup re-arms pending threads after `RunLifecycle.recover()` and before the first flush, and only when drafting runs are on.
- [x] A thread whose root a person marked as chatter, by `skip:` or the skip reaction (#56), is never re-armed. Its row is dropped at startup.

**Out of scope:**

- Retries of reconciling proposals (#30), which come next.

## Design

**Migration 6** adds `pending_threads (channel_id TEXT, thread_ts TEXT, due_at TEXT NOT NULL, PRIMARY KEY (channel_id, thread_ts))`. It holds pointers and a UTC time only.

**Store contract,** in `base.py`, for both `MemoryStore` and `SqliteStore`:

- `arm_thread(channel_id, thread_ts, due_at)` upserts the due time. A later message pushes it back.
- `pending_threads() -> list[PendingThread]` returns the pending threads, oldest due first.
- `queue_pending(channel_id, thread_ts) -> Run | None` does three things in one transaction. It enqueues the thread's drafting run, which coalesces with a queued one as today, and deletes the row. It returns None if there was no row, so a second flush of the same thread queues nothing.
- `drop_pending(channel_id, thread_ts)` deletes a row without queuing.

**`Debouncer` changes:**

- `note()` also calls `arm_thread` with the new due time.
- `flush()` calls `queue_pending` instead of `enqueue_run`.
- A new `restore()` loads `pending_threads()` into memory. It drops any whose root is a person's chatter, and returns how many it re-armed.
- Memory stays the source for `next_due()`, and the store is the durable copy.

**`Orchestrator._start`:** after `recover()` and the runner check, when drafting is on, it calls `debouncer.restore()` before the flush loop starts. It logs the count, and never ids or text.

**Why a recovered run doesn't double up.** `recover()` interrupts queued drafting runs and queues one fresh one per thread. A restored thread for the same thread then coalesces into it through the queued-drafting unique index. If that fresh run has already started, the store queues one follow-up. That is today's rule for a message arriving during a run, so a thread gets at most one extra run, never a burst.

**Review change: a skip after arming, and saving first.** Product, the lead and the reviewer agreed that a person's skip of a thread's first message cancels a thread already waiting, for example a skip reaction inside the window. `queue_pending` therefore checks the root inside its transaction. If a person has marked it chatter, it deletes the row, queues nothing, and returns None. `flush()` returns only the threads it actually queued. A skip on a reply doesn't cancel the thread. `note()` saves the due time before arming in memory, so a failed save never leaves a thread waiting in memory that then queues nothing.

## Risks and open questions

- **Clock changes:** due times are UTC. A time far in the past just flushes at once.
- **Row growth:** rows exist only while a thread waits, and flushing deletes them. A thread dropped as chatter is deleted at startup.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [x] Confirm the issue is ready
- [x] Commit this plan and open the draft PR
- [x] Migration 6 and the store contract, with contract and schema tests
- [x] Debouncer `note`, `flush`, and `restore`, with tests
- [x] Orchestrator startup wiring, with a restart test
- [x] Docs: the debouncer's docstring, and architecture's state section
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

`make check` is green, with 1765 tests. `debounce.py` and `service.py` are at 100% coverage.

- The store contract tests run against both stores. They cover arm, push back, ordering, `queue_pending` queuing once and coalescing with a queued run, drop, input checks, and rows surviving a reopen.
- The debouncer tests cover a waiting thread kept in the store, a restart re-arming it, a window that ended while down, a thread flushed before the restart not queued again, a person's skip dropped, and the agent's chatter still re-armed.
- The orchestrator test checks that a thread waiting at shutdown is drafted after start, and that a person-skipped one is not.
- The schema test covers `pending_threads`.
- Review change tests:
  - A skip reaction on the root inside the window gives no run and no row, through the real Bolt app and keyword handler. It fails without the store guard.
  - A skip on a reply still lets the thread run.
  - A failed save leaves nothing waiting in memory.
  - `queue_pending` skips a thread a person skipped, but not one the agent called chatter.

## Handoff notes

Worktree `~/jotflow-work/wt-security-timers`, branch `feat/persist-debounce`.
