# Keep quiet-window timers across a restart

## Links and status

- Issue: #130
- Also serves: #19, the debouncer this extends
- Draft PR: pending
- Status: planning
- Last verified: plan only
- Next safe action: the lead confirms the plan, then implement

## Objective and done-when

The debouncer (#19) keeps each thread's due time in memory. A thread waiting out its quiet window when JotFlow stops gets no drafting run until someone posts again, because Slack does not redeliver messages after a restart. This issue keeps those due times in the store and re-arms them at startup.

The issue's boxes, refined:

- [ ] A thread pending at shutdown is queued for its drafting run after a restart, once its due time has passed. A thread whose due time passed while JotFlow was down is queued on the first flush.
- [ ] Pending entries hold no message text: only the channel id, the thread ts, and the due time. The schema test covers the new table.
- [ ] A thread flushed or run before the restart is not queued twice. Flushing queues the drafting run and deletes the pending row in one store transaction, so a crash can't leave both.

From the lead:

- [ ] Startup re-arms pending threads after `RunLifecycle.recover()` and before the first flush, and only when drafting runs are on.
- [ ] A thread whose root a person marked as chatter, by `skip:` or the skip reaction (#56), is never re-armed. Its row is dropped at startup.

**Out of scope:**

- Re-checking chatter at flush time for a thread armed in this process. That changes today's debouncer behaviour, which is #56's call, so I'll raise it with product.
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

## Risks and open questions

- **Clock changes:** due times are UTC. A time far in the past just flushes at once.
- **Row growth:** rows exist only while a thread waits, and flushing deletes them. A thread dropped as chatter is deleted at startup.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [x] Confirm the issue is ready
- [ ] Commit this plan and open the draft PR
- [ ] Migration 6 and the store contract, with contract and schema tests
- [ ] Debouncer `note`, `flush`, and `restore`, with tests
- [ ] Orchestrator startup wiring, with a restart test
- [ ] Docs: the debouncer's docstring, and architecture's state section
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

Pending.

## Handoff notes

Worktree `~/jotflow-work/wt-security-timers`, branch `feat/persist-debounce`.
