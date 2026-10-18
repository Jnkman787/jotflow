# Execution retries under a limit

## Links and status

- Issue: #30
- Draft PR: pending
- Status: planning
- Last verified: plan only
- Next safe action: the lead confirms the defaults in Risks, then implement

## Objective and done-when

Since #131, an execution run that ends without `record_result` leaves its proposal reconciling. Today only a restart queues the next attempt (#104). This issue retries in process, under a limit, and gives up cleanly at the limit.

The issue's boxes, with what is already done:

- [ ] Approved proposal yields exactly one execution run. The store already coalesces execution runs per proposal through its unique index. Approval in Slack is M4's card flow. Here, a test shows that a retry and a restart recovery together still leave one live run per proposal.
- [x] `record_result` with a URL moves the proposal to `applied` (#15). It's shown end to end in #136.
- [ ] Failure or timeout is reported in the thread, and the proposal is retried. Each failed attempt already posts the fixed run notice (#131). This adds the retry. The issue's wording, "returns to approved", predates the reconciling state. The proposal stays **reconciling**, so every retry checks the hidden marker before writing.

From the #30 note:

- [ ] A reconciling proposal gets a fresh execution run when its run ends, while it has had fewer than the limit of attempts.
- [ ] At the limit it moves to **failed**, with a fixed card notice. The card shows `FAILED_LINE` if it has one, and a fixed thread notice goes out otherwise. No agent text is shown. A person can still retry a failed proposal by approving it again, through the existing failed → approved edge.
- [ ] The limit also holds across restarts. A proposal that reached it is failed at its next start, not run again.

**Out of scope:** approval in Slack, and posting the first card (M4). Configuring the limit in `jotflow.toml`, unless the lead wants it now.

## Design

**Counting attempts.** An attempt is an execution run for the proposal that has ended, by any status, interrupted included, so a crash loop is bounded too. It is counted from `store.thread_runs(channel, thread)`, filtered by kind and `proposal_id`, so no new store method is needed. The limit is `MAX_EXECUTION_ATTEMPTS = 3`.

**`RunLifecycle` changes:**

- `finish()` already moves an executing proposal to reconciling. Then, if the proposal is reconciling, it either queues a fresh execution run, when attempts are under the limit, or gives up. Giving up is a `try_transition` from reconciling to failed with `expected={RECONCILING}`, followed by the `on_gave_up` callback.
- `_prepare_execution()` gives up the same way, without starting, for a reconciling proposal already at the limit. That covers a run that restart recovery queued.
- `on_gave_up: Callable[[ProposalRecord], Awaitable[None]] | None` is injected, so the lifecycle stays free of Slack. Its errors are logged and never raised.

**The notice,** a new `GiveUpNotice` in `orchestrator/runs_notice.py`, wired by the orchestrator:

- When the proposal has a `card_ts` and a `CardPublisher` is available, it re-renders the card, which shows `FAILED_LINE` in the failed state.
- Otherwise it posts one fixed thread reply: "JotFlow stopped trying to write this proposal after 3 attempts. Nothing more will be written unless someone approves it again."
- It never includes the agent's error text. That stays in the store, as the card's comment already says.

## Risks and open questions

- **Limit value and where it lives.** My default is 3 total attempts, as a constant. It could become `[agent] max_execution_attempts` later.
- **Card context.** No card is posted in the live path yet. Re-rendering needs a `CardContext`, so I'll build a minimal one from the channel policy. That shows Slack ids as mentions rather than cached names. If the M4 card flow lands first, I'll use its context builder.
- **The architecture diagram** says "reconciling → approved: marker absent, retry allowed". I'll change that edge to say that a retry stays reconciling, so the marker check always runs.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [x] Confirm the issue is ready
- [ ] Commit this plan and open the draft PR
- [ ] Lifecycle: retry or give up in `finish` and in `_prepare_execution`, with tests in both stores
- [ ] The notice: card or fixed thread text, wired in the orchestrator, with tests
- [ ] A restart test where the limit holds across recovery
- [ ] Docs: architecture's run lifecycle section and diagram
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

Pending.

## Handoff notes

Worktree `~/jotflow-work/wt-security-retry`, branch `feat/execution-retries`. It is independent of #145.
