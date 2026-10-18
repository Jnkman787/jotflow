# Issue and revoke run tokens through the run lifecycle

## Links and status

- Issue: #107
- Also serves: #16 (per-run tokens, merged in #105), #53 (run records, merged in #104)
- Draft PR: #116
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18
- Next safe action: review; the adapter (#21) calls `start_next`, `finish`, and `expire_overdue` from its loop

## Objective and done-when

Run records (#104) and per-run tokens (#105) exist, but nothing connects them. This adds the one place that starts and ends runs, so every run gets exactly one token while it may act, and loses it the moment it stops.

Done when:

- [x] Starting a run issues exactly one token; finishing, timing out, or recovery revokes it; tests cover each path
- [x] A token's expiry follows its run's deadline plus a short grace, and never exceeds the 2-hour cap
- [x] The maximum run timeout is capped at 2 hours through the shared constant. Already true on main: #104 set `MAX_RUN_TIMEOUT_SECONDS = MAX_TOKEN_TTL_SECONDS`. This PR adds a test that pins it.
- [x] No token is written to the store or the logs
- [x] `make check` passes, including the coverage floor

**Out of scope:** launching the agent process (#21) and the loop that calls this on a timer, which comes with the adapter. Persisting tokens: they stay in memory by design (#105), and a restart re-queues runs, which then get fresh tokens.

## Design

`src/jotflow/orchestrator/runs.py`, one class, `RunLifecycle(store, registry, clock=utc_now)`:

- `start_next(max_running)` claims the next run (`Store.claim_next_run`), builds its `Grant`, and issues its token. The token lasts `deadline - now + GRACE_SECONDS`, 60 seconds, and never more than `MAX_TOKEN_TTL_SECONDS`. It returns a `StartedRun(run, token)`, whose `repr` hides the token, or `None` when nothing can start. If issuing fails after the claim, the run is finished as failed with a fixed reason, so it never sits running without a token.
- `finish(run_id, status, error=None)` revokes first, then calls `Store.finish_run`. Revoking first means a run that is being stopped can make no more tool calls, even if the store write is slow.
- `expire_overdue()` revokes and finishes as `timed_out` every run past its deadline (`Store.overdue_runs`).
- `recover()`, at startup, calls `Store.recover_runs` and revokes any token of a run it marked interrupted. That is a no-op after a real restart, since tokens are in memory, but it keeps the rule true when recovery runs in a live process.
- `grant_for(run)` maps a run to its grant: kind, channel, and thread, plus the proposal for execution runs.

No token is stored or logged. The registry keeps only digests (#105). `StartedRun.token` is excluded from `repr`, and the module logs run ids only. Tests scan the SQLite file and captured logs for the token.

## Risks and open questions

- The grace period. 60 seconds lets an agent's last `record_result` land just after the deadline, while the run is being timed out. It is a constant, easy to change.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `RunLifecycle` and `grant_for`
- [x] Tests: each path, expiry and cap, timeout cap, no token in store or logs
- [x] Docs: architecture Run lifecycle, AGENTS.md layout
- [x] CodeRabbit once, or skipped: see Validation
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `tests/test_runs.py` runs against MemoryStore and SqliteStore, with one fake clock for both the store and the registry. It covers:
  - Starting issues exactly one token, with the right grant for drafting and for execution runs; nothing to start issues none.
  - Finishing, failing, timing out, and recovery each revoke the token; a run another path already ended is not reported as timed out.
  - The token lasts exactly to the deadline plus grace, its lifetime is capped at 2 hours and never zero, and a run timeout over 2 hours is refused.
  - A token failure after the claim fails the run with a fixed reason.
  - The token is in no database file, no log line, and no `repr`.
- CodeRabbit: skipped. The CLI failed to connect ("Provider identity could not be verified").

## Handoff notes

None yet.
