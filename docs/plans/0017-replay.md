# `jotflow replay`: a saved thread through the real pipeline

## Links and status

- Issue: #17
- Also serves: #18 (its last box, "used by the replay tests")
- Draft PR: #111
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18 on main with #106: 1268 tests, `replay.py` and the command at 100%
- Next safe action: review; when #20's keyword parser lands, switch the keyword check from not run to real

## Objective and done-when

`jotflow replay <thread>` runs a saved thread through JotFlow's real ingest, store, and tool layer, with the fixture's scripted fake agent in place of a real one and no Slack or network (D4). It is how the pipeline is tested in CI, and it doubles as a demo.

Done when:

- [x] Replaying each built-in fixture produces exactly its expected topics, chatter, proposals, and asks, compared against `expected` in the fixture
- [x] Runs in CI with no network access: a test replays every fixture with sockets blocked
- [x] Usable as a demo from the README: `uv run jotflow replay feature-request` prints a short, readable report
- [x] The fixtures are used by the replay tests (#18's last box)
- [x] `make check` passes, including the coverage floor

**Out of scope:** a terminal agent listing the replayed topics over MCP. That half of M2's done-when needs a token a person can hold, which is the operator token in M5 (D12). This PR leaves a SQLite file a later step can serve. Debounce timing (#19): replay starts each scripted run directly, in script order.

## Design

The pipeline, per thread, all in-process:

1. **Load.** `load_thread`, or `builtin_thread` for a bare name. Build the `Policy` from the fixture's `policy`. Open a `MemoryStore`, or a `SqliteStore` with `--db PATH`. Seed it with `existing_topics` and the fixture users' GitHub links as identity links.
2. **Ingest.** Pass every event through the real `EventFilter`, with the fixture's bot identity. Record what is accepted and ignored, with reasons, and compare against `expected.accepted` and `expected.ignored`.
3. **Keywords.** `re:` and `skip:` are JotFlow's own doing (D10). Replay applies them through the keyword parser once it exists. Until then, the keyword expectations are reported as not checked, never as passed.
4. **Runs.** For each run in the agent script:
   - create a drafting `Run` in the store (#104), and issue a `Grant` for its channel and thread (#105)
   - call `get_work` through the read tool handler (#106), with a `WorkSource` that serves the fixture's accepted messages for that thread
   - make each scripted call through the write tool handlers (#109), under the same grant
   - mark the run finished
5. **Fakes.** A `ThreadPoster` that records `ask_channel` posts instead of sending them. Topic keys: `key:<name>` in the script resolves to the topic id the store assigned when that `link_message` created the topic.
6. **Compare.** Read the store and compare it against `expected`: each topic and its messages, chatter, each proposal's kind, repo, topic, and issue number, and asks by thread. The result is a `ReplayReport` listing each check as ok or with the difference.

The command:

- `jotflow replay <file or built-in name>` prints the report and exits 0 when everything matches, 1 otherwise.
- `--list` names the built-in fixtures, and `--db PATH` keeps the store in a SQLite file.
- Output follows the redaction rule: the report names messages by `ts`, never by their text.

Files: `src/jotflow/replay.py` holds the pipeline and report, and `src/jotflow/commands/replay.py` the CLI. Tests are in `tests/test_replay.py`. The README gains a short "See it without Slack" section.

### Checked against merged #109 (2026-10-18)

The write tools are `WriteTools(store, policy, poster)`, with handlers `propose_issue`, `propose_update`, `link_message`, and `ask_channel`, each taking `(grant, arguments)`. `ThreadPoster.post(channel_id, thread_ts, text)` is the protocol replay fakes. Two differences from the fixture format need bridging:

- **`link_message` takes `LinkMessageArgs`:** the `ts`, the decision, a `topic_id` for an existing topic, and a `title` for a new one. The fixture's link calls carry a `Classification`, which has a message ref but no title. Replay converts the ref to its `ts` and resolves `key:` ids. For a new topic it needs a title, so the fixture format gains an optional `title` on `LinkMessageCall`. The loader requires it when the decision is `new_topic`, and every built-in fixture gets one. This is an additive change within `jotflow-thread/1`.
- **`link_message` only accepts a `ts` the run's packet served** (`Store.packet_ref`). `get_work` (#106) records the packet with `Store.record_packet`. Replay goes through `get_work` once #106 merges; until then the design does not change, only the step that records the packet.

Proposals take `ProposeIssueArgs` and `ProposeUpdateArgs`, the fixture models plus `participants`. Replay fills `participants` from the users whose messages are in the thread.

## Risks and open questions

- This depends on four open PRs. If their merged interfaces differ from their plans, this design follows the merged code and this plan is updated.
- The keyword parser has no PR yet; see step 3.
- Replay calls tool handlers in-process, not over HTTP. The HTTP path is #13's tests' job; replay tests the pipeline's logic.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Wait for #104, #105, #106, #109; re-check this design against them
- [x] Pipeline and report in `replay.py`
- [x] CLI command
- [x] Tests: every fixture, sockets blocked, CLI output
- [x] README demo section
- [x] CodeRabbit: skipped, rate-limited
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

Built as designed, with these specifics:

- Each scripted run goes through `RunLifecycle.start_next`, which issues its token. Its grant comes from `TokenRegistry.check`, and tools are called through their `ToolSpec`, refusing a tool the run's kind may not call. Then `finish` ends the run.
- Existing topics are seeded with store-assigned ids and mapped from the fixture's ids.
- Topic comparisons count only the replayed thread's messages, because an existing topic already holds older ones.
- A refused scripted call fails the `script` check, and the comparison still runs, so the report shows what differs.

Tests:

- `tests/test_replay.py`: every built-in fixture matches its expected outcome, with sockets blocked. Also covered:
  - the keyword check reported as not run on `keywords.json` only
  - the real SQLite store holding the proposal and a succeeded run
  - a mismatch from a reply ingest drops
  - a refused tool call
  - the report never containing message text
  - the command's exit codes, `--list`, `--db`, and usage errors
  - the internal error paths
- `tests/test_fixture_threads.py`: a new-topic link needs a title, and only it takes one.

The fixture format gains `LinkMessageCall.title`, and the seven new-topic link calls across six fixtures carry one. The README gains "See it work without Slack".

## Handoff notes

When #20 adds `orchestrator/keywords.py`, apply `split_keyword` in replay's ingest step for `re:` and `skip:`, then turn the keyword check from not run into a real comparison. Do not write a second parser.
