# The issue URL in the thread, and a reaction on the source messages

## Links and status

- Issue: #32
- Also serves: #38 (`jotflow rebuild` reads the URL in the thread), #6 (`issue_links`), #152 (the outbox pattern), D4
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-19, no code yet
- Next safe action: the store's thread-told mark, once the architect confirms the policy key

## Objective and done-when

When a proposal is applied, JotFlow says so where the idea came from. It replies in the proposal's thread with the issue URL, and it reacts on each source message. With the stored links, that reply makes the store a rebuildable cache, even under `issue_links = "none"`, where the issue body holds no Slack details (#31).

Per the lead's update on the issue, box 1, permalinks in the issue body, moved to #31.

Done when:

- [ ] An applied proposal gets one thread reply holding the issue URL, for a new issue and for an update
- [ ] Each of its source messages gets the configured reaction, and the reaction can be turned off
- [ ] Both are driven by the proposal reaching `applied`, through the store's outbox, never by the agent and never inside an MCP tool's write
- [ ] A Slack failure is retried without ever posting the reply twice, and a paused channel waits
- [ ] With `issue_links = "none"` the reply still carries the URL, and nothing in it is agent-written
- [ ] `make check` passes, including the coverage floor

**Out of scope:** the issue body itself, which is #31's, and `jotflow rebuild`, which is #38's.

## Design

- **The outbox.** Migration 9 adds `proposals.thread_told_at`, null until the thread is told.
  - `Store.threads_due(limit)` lists applied proposals not yet told, oldest first, and leaves out paused channels, as `cards_due` does.
  - `Store.mark_thread_told(id)` sets it once.
  - On upgrade, every proposal already applied counts as told. Nothing told threads before #32, and an old thread should not suddenly get a reply.
  - Both stores implement it, with contract and schema tests.
- **`orchestrator/thread_notice.py`**, `ThreadNotice(store, policy, slack)`. `tell()` takes each due proposal:
  1. It reacts with the configured emoji on each source message. Slack's `already_reacted` counts as done, and `message_not_found` skips that message.
  2. It replies in the thread: "Created web#7: https://github.com/acme-co/web/issues/7", or "Updated web#42: ...". The nickname comes from the channel's policy, and the URL is the one `record_result` validated against the approved repo. The reply is plain text that JotFlow builds itself, with links unfurled off.
  3. It records `thread_told_at`.
  - Reactions go first because they are idempotent, so a retry repeats them harmlessly but never repeats the reply. Only a failure of step 3 itself could double the reply.
  - Failures back off from 2 seconds to a minute, as `CardSync` does. Errors that retrying cannot fix, such as the thread or channel being gone, mark it told and log.
  - It runs in the orchestrator's card loop, after `CardSync.sync`.
- **The policy key** (asked of the architect): a top-level `issue_reaction`, defaulting to `"ticket"`. An empty string means no reaction. The value is checked as a Slack emoji name, and must not be the keyword confirmation `white_check_mark`. It goes in the example file and `policy_writer`, with round-trip and refusal tests.
- **Scope.** The reaction needs `reactions:write`, which the manifest already has. The reply needs `chat:write`.

## Risks and open questions

- The policy key and its default await the architect.
- **Migration numbers.** Nothing open uses 9 today. Whichever lands later renumbers.
- An update proposal's sources may include messages from another thread. Each gets the reaction, and the reply goes to the proposal's own thread.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; the policy key pending the architect
- [ ] Commit this plan and open the draft PR
- [ ] Store: migration 9, `threads_due`, `mark_thread_told`, both stores, contract and upgrade tests
- [ ] Policy: `issue_reaction`, with the example file, the writer, and tests
- [ ] `thread_notice.py`: reactions, reply, mark, backoff, final errors
- [ ] Orchestrator: run it in the card loop
- [ ] Tests through the real orchestrator: an applied proposal gets one reply and its reactions; a retry never doubles the reply; a paused channel waits
- [ ] Docs: architecture's execution section, README
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
