# Debouncer: a per-thread quiet window and loose messages

## Links and status

- Issue: #19
- Also serves: #53 (runs are queued through the store's one-run-per-thread rule), D9, #8
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: write `src/jotflow/orchestrator/debounce.py`

## Objective and done-when

A thread is handed to the agent only after it has been quiet for the channel's window, so the first draft reflects a whole burst of replies instead of the first message alone.

Done when:

- [ ] A burst of replies within the window yields one drafting run, not several
- [ ] Loose top-level messages follow the documented strategy below
- [ ] The window is configurable per channel in `jotflow.toml`
- [ ] The bot's own messages never re-arm a thread's timer (#8)
- [ ] A reply in a thread whose topic has expired revives that topic (D9)
- [ ] `make check` passes, including the coverage floor

**Out of scope:** wiring the debouncer into `jotflow run` with its Slack listeners, which comes with #55 along with the run loop. Starting runs: the debouncer only queues them, and `RunLifecycle.start_next` starts them (#107).

## Design

- **The window.** Nothing new in `jotflow.toml`. The window is already there: top-level `debounce_seconds` (default 120), overridden per channel by `[channels.<id>] debounce_seconds`, read with `Policy.debounce_for(channel)`. The architect owns the policy file and has been asked to confirm. The third done-when box is met by that existing setting, and this PR tests that the debouncer honours it.
- **`Debouncer(store, policy, clock)`**, in `src/jotflow/orchestrator/debounce.py`:
  - `note(decision)` takes an ingest decision. Only an accepted message counts; reactions, ignored events, and the bot's own messages never reach it, because ingest drops them first (#8). It works out the thread: a reply's `thread_ts`, or a top-level post's own ts (#104). It sets that thread's due time to the message's arrival plus the channel's window, so every new message pushes the due time back.
  - `flush(now)` queues a drafting run, `Store.enqueue_run(drafting, channel, thread)`, for every thread whose due time has passed, and forgets it. The store coalesces a burst that lands during a running run into one queued run (#104), so a thread never runs twice at once.
  - `next_due()` says when the next thread falls due, so the loop in #55 can sleep exactly that long.
- **Loose top-level messages: one candidate topic each.** Every top-level post is its own thread with its own timer, and gets its own drafting run once quiet. The agent then attaches it to an existing topic or starts a new one, using the topic index and the related excerpts (D9). Grouping loose posts by time or author was considered and rejected: two people posting about different things a minute apart would be merged, and correlating on content is the agent's job, not a timer's.
- **Reviving an expired topic.** When a reply arrives and the thread's top-level message is already linked to a topic, the debouncer links the reply to that topic too, as an agent-level decision a person can still override. Linking refreshes the topic's activity, so the topic is back in the index the run receives, even if it had aged out.

## Risks and open questions

- **Timers live in memory.** A thread that was waiting out its window when JotFlow stopped gets no run until its next message, because Slack does not redeliver. Persisting pending threads in the store is a small follow-up if it matters in practice; I'll raise it as an issue rather than widen this PR.
- **Reactions do not start runs in v1.** Keyword reactions arrive with #56.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] `Debouncer`: `note`, `flush`, `next_due`, reviving
- [ ] Tests: bursts, loose posts, per-channel windows, bot messages, revival, coalescing with a running run
- [ ] Docs: architecture Conversation rules, AGENTS.md layout
- [ ] Also in this PR: the reviewer's optional nit on #111, so replay reports never quote a scripted call's input
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
