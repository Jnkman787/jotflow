# The issue URL in the thread, and a reaction on the source messages

## Links and status

- Issue: #32
- Also serves: #38 (`jotflow rebuild` reads the URL in the thread), #6 (`issue_links`), #152 (the outbox pattern), D4
- Draft PR: #157, stacked on #155 (`feat/channel-modes`) for its paused-channel rule
- Status: implementing
- Last verified: 2026-10-19, `make check` passes with the code below
- Next safe action: CodeRabbit, then ready; rebase onto main once #155 merges

## Objective and done-when

When a proposal is applied, JotFlow says so where the idea came from. It replies in the proposal's thread with the issue URL, and it reacts on each source message. With the stored links, that reply makes the store a rebuildable cache, even under `issue_links = "none"`, where the issue body holds no Slack details (#31).

Per the lead's update on the issue, box 1, permalinks in the issue body, moved to #31.

Done when:

- [x] An applied proposal gets one thread reply holding the issue URL, for a new issue and for an update
- [x] Each of its source messages gets the configured reaction, and the reaction can be turned off
- [x] Both are driven by the proposal reaching `applied`, through the store's outbox, never by the agent and never inside an MCP tool's write
- [x] A Slack failure is retried without ever posting the reply twice, and a paused channel waits
- [x] With `issue_links = "none"` the reply still carries the URL, and nothing in it is agent-written
- [x] `make check` passes, including the coverage floor

**Out of scope:** the issue body itself, which is #31's, and `jotflow rebuild`, which is #38's.

## Design

- **The outbox.** Migration 9 adds `proposals.thread_told_at`, null until the thread is told.
  - `Store.threads_due(limit)` lists applied proposals not yet told, oldest first, and leaves out paused channels, as `cards_due` does.
  - `Store.mark_thread_told(id)` sets it once.
  - On upgrade, every proposal already applied counts as told. Nothing told threads before #32, and an old thread should not suddenly get a reply.
  - Both stores implement it, with contract and schema tests.
- **`orchestrator/thread_notice.py`**, `ThreadNotice(store, policy, slack)`. `tell()` takes each due proposal:
  1. It reacts with the configured emoji on each source message. Slack's `already_reacted` counts as done, and `message_not_found` skips that message.
  2. It replies in the thread: "Created web#7: https://github.com/acme-co/web/issues/7", or "Updated web#42: ...". The nickname comes from the channel's policy, and the URL is the one `record_result` validated against the approved repo. The reply is mrkdwn built only by JotFlow: `<url|web#7>`, where the URL must match `https://github.com/owner/name/issues/N` again here, below the model's own check, and the nickname comes from policy. Links never unfurl.
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
- [x] Commit this plan and open the draft PR
- [x] Store: migration 9, `threads_due`, `mark_thread_told`, both stores, contract and upgrade tests
- [x] Policy: `issue_reaction`, with the example file, the writer, and tests
- [x] `thread_notice.py`: reactions, reply, mark, backoff, final errors
- [x] Orchestrator: run it in the card loop
- [x] Tests through the real orchestrator: an applied proposal gets one reply and its reactions; a retry never doubles the reply; a paused channel waits
- [x] Docs: architecture's execution section, README
- [ ] CodeRabbit once, or skipped
- [x] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- `make check` passes.
- `tests/test_thread_notice.py` runs on both stores. It covers:
  - one reply and one reaction per distinct source message, with nothing on the next pass
  - "Updated" for an update, the reaction turned off, and the URL kept under `issue_links = "none"`
  - already-reacted and missing messages, and a failed reply retried and posted once
  - a failed reaction retried before any reply, gone threads given up, and logs without error text
  - a paused channel waiting
  - only a GitHub issue URL ever linked, and the adapter's no-unfurl calls
- The store's contract and upgrade tests cover `threads_due` and `mark_thread_told`, and show proposals applied before the upgrade are not told again.
- The policy tests cover the default, `""`, a refused `white_check_mark`, bad names, and a round trip through the writer.
- `tests/test_service.py` shows the running service telling an applied proposal's thread, with its reactions.

## Handoff notes

None yet.
