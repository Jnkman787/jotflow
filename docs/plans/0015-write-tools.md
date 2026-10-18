# MCP write tools: propose_issue, propose_update, link_message, ask_channel, record_result

## Links and status

- Issue: #15
- Draft PR: #109
- Status: ready for review
- Last verified: rebased on main at 34a0c64 (#104, #112); `make check` green, 1017 tests, 98.4% coverage
- Next safe action: reviewer reviews #109

## Objective and done-when

The agent's only way to act. Each call is checked against the run's grant, recorded before the tool returns, and safe to repeat.

The issue's boxes:

- [x] Every call is persisted before the tool returns
- [x] Proposals outside the run's channel or the channel's repo and label policy are rejected with a reason
- [x] `record_result` is accepted only for the run's own approved proposal

From the dated notes on #15:

- [x] **Idempotent.** A call repeated after a timeout returns the first call's result and writes nothing new. A store write is never cancelled halfway.
- [x] **Participants** on `propose_*` are separate from assignees. They are informational and never written to GitHub as assignees.
- [x] **`link_message`** touches only messages that were in the run's own packet
- [x] **Per-run caps** on `propose_*` and `ask_channel`
- [x] **`record_result`** applies only to the grant's own proposal, needs an issue URL that matches the approved repo and issue, and works once. A second call is refused, and a repeat of the same call returns the first result.
- [x] **`ask_channel`** posts only into the run's own thread, strips broadcast mentions, and is rate-limited
- [x] Each tool is available only to the run kinds the token table allows

**Out of scope:** the read tools and `get_work` (#14), fetching threads (#20), cards and approval (#26 and later), the execution run's GitHub write itself (#30).

## Design

References: D1, D5, D7, D8, and D13 in [roadmap.md](../roadmap.md); the tool and token tables, Run lifecycle, and Conversation rules in [architecture.md](../architecture.md); the plans for #13, #14, and #16.

**Who may call what**, from the token table:

| Tool | Drafting | Execution | Question (M5) |
|---|---|---|---|
| `propose_issue`, `propose_update` | yes | no | no |
| `link_message` | yes | no | no |
| `ask_channel` | yes | no | yes |
| `record_result` | no | yes | no |

**Scoping.** The channel, thread, run, and for execution runs the proposal all come from the `Grant`, never from arguments. A message, topic, or proposal outside the grant reads as "not found", the same as a missing one, so ids cannot be probed across channels or runs.

**Idempotency.** The key agreed on #15 is the run id plus a per-run call counter. On its own a counter cannot spot a retry: the agent's second attempt is a new MCP request. So the ledger key is the run id plus a hash of the tool name and its canonical arguments, and the per-run counter numbers each recorded call for caps and audit.

- A repeat of an identical call returns the stored result and writes nothing.
- A different call is a new entry and counts against the caps.
- The store write and the ledger entry commit in one transaction, inside a cancellation-shielded scope, so the 30 second request timeout cannot cut a write in half. If the timeout fires after the commit, the agent's retry gets the stored result.

**`propose_issue` and `propose_update`.** Each validates the draft against the channel's policy: the repo by name or nickname, and labels through `check_labels`. Assignees must be linked GitHub logins. Participants must be Slack user ids, are stored on the record, and are never turned into assignees. Each call adds a pending `ProposalRecord` for the grant's channel and thread, run id, and a random id that doubles as the hidden marker. It then supersedes the thread's older pending proposals (D8). At most 3 per run.

**`link_message`.** The arguments are the message ts, a decision (`new_topic`, `existing_topic`, or `chatter`), a topic id or new title, and a confidence. The ts must be one the run's `get_work` packet served. A person's earlier decision is never overwritten; the store already enforces that. A new topic's title goes through `clean_title`. At most 200 per run.

**`ask_channel`.** The text is at most 500 characters. Broadcast mentions (`<!channel>`, `<!here>`, `<!everyone>`, `<!subteam^…>`, and the bare `@channel`, `@here`, `@everyone` forms) are removed, and `&`, `<`, and `>` are escaped, so the text renders as plain text. It posts only to the grant's thread, through a `ThreadPoster` protocol that `jotflow run` implements with the Slack client and tests fake. At most 2 per run, and at most one every 30 seconds per thread across runs.

**`record_result`.** Execution kind only. The proposal is `grant.proposal_id`, which must be executing. A result with an issue URL must match the proposal (`ExecutionResult.matches`) and moves it to applied. A result with an error moves it to failed. The state change is the store's atomic transition, so a second, different `record_result` is refused. An identical repeat returns the first result through the ledger.

**Store additions.** These come in a new migration after #104's.

- `tool_calls`: run id, sequence number, tool, argument hash, result JSON, time. The result JSON holds ids and states, never message text.
- A record of which message ts values each run's packet served, written by `get_work` (#14) and read by `link_message`.

**Shared edits with #14**, agreed with architect on 2026-10-18:

- `whiteboard/tools.py`: #14 adds `ToolError`, which the server turns into `CallToolResult(is_error=True)`, and `READ_TOOLS`. This PR adds `WRITE_TOOLS`, and `TOOLS` becomes both. Whichever PR merges second rebases; the conflict is one line.
- `server.py`: #14 owns the `ToolError` path and `output_model`. This PR changes nothing there.
- Packet membership: a `packet_messages(run_id, channel_id, ts)` table with `record_packet(run_id, refs)` and `in_packet(run_id, channel_id, ts)`. A drafting `get_work` records exactly the refs it serves. Execution packets serve no messages and record none. Whichever of #106 and #109 merges first adds the table and its migration, after #104's runs migration, and the other uses it.
- Idempotency: architect agreed the ledger key is run id plus the hash of tool and canonical arguments, with the counter only for caps and the write shielded.

Alternatives considered:

- A client-chosen call id: depends on the model remembering to reuse it on a retry. Hashing the arguments needs nothing from the agent.
- Posting `ask_channel` from the tool through Slack directly with no protocol: harder to test, and it would tie the tool layer to Bolt.

## Risks and open questions

- #104 merged first, so its runs migration is 3 and this PR's ledger and packet migration is 4.
- Wiring is left to #107. `jotflow run` does not open the store yet, so `write_tools(...)` and `SlackThreadPoster` ship tested but are not passed to the server. #107 opens the store, issues tokens, and passes `(*read_tools(...), *write_tools(...))` to `build_app`. Until then no token exists, so production behaviour does not change.
- `record_result` requires the proposal to be executing. The run lifecycle (#107) moves it from approved to executing when it claims the execution run.

- Packet membership needs #14's `get_work` to record what it served. Until both land, `link_message` refuses everything, so it fails closed.
- The caps (3 proposals, 2 questions, 200 links per run, one question per 30 seconds per thread) are first guesses, in one constants block.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Rebase on `main` once #104 and #105 merged
- [x] Migration: `tool_calls` and packet membership; store methods on both stores
- [x] Ledger with shielded commit and argument-hash idempotency
- [x] The five tools with kinds, scoping, caps, and policy checks
- [x] `ThreadPoster` with the Slack implementation in `jotflow run`, and broadcast stripping
- [x] Tests for every rule, through the real endpoint where the rule is about the wire
- [x] Update documentation: architecture tool table and write-tool rules
- [x] `make check` passes
- [x] CodeRabbit local review, or skipped: skipped, rate limit
- [x] `closingIssuesReferences` checked, then mark the PR ready

## Validation

- CodeRabbit: skipped. The free CLI allowance was used up ("Rate limit exceeded").
- `make check` passes: ruff, mypy strict, and 908 tests with 98.3% total coverage. `write_tools.py` is at 98%.
- Store, in `tests/test_store_contract.py` for both stores: calls numbered per run and found by hash, a duplicate record refused, counts per run and per thread over time, and packet refs per run with their own thread. `test_store_schema.py` covers the two new tables, and no column holds message text.
- `tests/test_write_tools.py`, 55 tests:
  - `link_message`: new, existing, and chatter decisions. A message outside the run's packet, or in another channel, is refused. A person's decision stands with no orphan topic left behind. A topic in another channel reads as missing. A title that cleans to nothing is refused, argument shapes per decision are checked, and the cap holds.
  - idempotency: an identical repeat returns the first result with one ledger entry, and another run's identical call is new. Five concurrent identical calls write once. A timeout during a slow post still finishes the post and its ledger entry, and the retry returns that result without posting again.
  - `propose_issue`: the grant's channel, thread, and run. The policy's repo and label spelling. Sources rewritten from the packet. Participants deduplicated, and the older pending proposal superseded. Refused: a missing or other-channel topic, a disallowed repo or label, a source outside the packet, an assignee who is not a linked participant, a topic that already has an issue, and the cap.
  - `propose_update`: only the topic's own repo and issue, and the same assignee rule
  - `ask_channel`: posts only to the grant's thread. Broadcasts stripped, markup escaped, and control characters dropped. Refused: an empty question, a second question within 30 seconds from another run, the per-run cap, and a failed post, which records nothing so it can be retried.
  - `record_result`: a matching URL applies and links the topic, and an identical repeat returns the first result. A second, different result is refused. Another repo or a comment URL is refused, an error result fails the proposal, a non-executing proposal is refused, and a proposal outside the grant reads as missing.
  - the registry kinds, and the Slack poster's flags and error mapping
  - over the wire: a drafting token lists the four drafting tools, an execution token lists only `record_result`, and a refusal arrives as `isError: true` with the code-written reason.

## Handoff notes

- #107: pass `write_tools(store, policy, SlackThreadPoster(app.client))` alongside the read tools into `build_app(tools=...)`. Move a proposal to executing when claiming its execution run, and revoke the run's token when it ends.
- #14: `get_work` for drafting calls `store.record_packet(run_id, refs)`, with each ref's own `thread_ts`. Related-thread excerpts included only if linking them should be allowed. `ToolError` and its result path are now in `tools.py` and `server.py` in the shape the #14 plan described, so rebase onto them.
- The caps are constants at the top of `write_tools.py`.
