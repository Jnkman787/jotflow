# `jotflow replay`: a saved thread through the real pipeline

## Links and status

- Issue: #17
- Also serves: #18 (its last box, "used by the replay tests")
- Draft PR: to be opened
- Status: blocked on #104 (run records), #105 (grants and tokens), #106 (read tools), #109 (write tools)
- Last verified: plan written against the plans of the PRs above, no code yet
- Next safe action: once those merge, write `src/jotflow/replay.py` against their merged interfaces and check this design still matches

## Objective and done-when

`jotflow replay <thread>` runs a saved thread through JotFlow's real ingest, store, and tool layer, with the fixture's scripted fake agent in place of a real one and no Slack or network (D4). It is how the pipeline is tested in CI, and it doubles as a demo.

Done when:

- [ ] Replaying each built-in fixture produces exactly its expected topics, chatter, proposals, and asks, compared against `expected` in the fixture
- [ ] Runs in CI with no network access: a test replays every fixture with sockets blocked
- [ ] Usable as a demo from the README: `uv run jotflow replay feature-request` prints a short, readable report
- [ ] The fixtures are used by the replay tests (#18's last box)
- [ ] `make check` passes, including the coverage floor

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

## Risks and open questions

- This depends on four open PRs. If their merged interfaces differ from their plans, this design follows the merged code and this plan is updated.
- The keyword parser has no PR yet; see step 3.
- Replay calls tool handlers in-process, not over HTTP. The HTTP path is #13's tests' job; replay tests the pipeline's logic.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Wait for #104, #105, #106, #109; re-check this design against them
- [ ] Pipeline and report in `replay.py`
- [ ] CLI command
- [ ] Tests: every fixture, sockets blocked, CLI output
- [ ] README demo section
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

Blocked as above. Nothing is implemented yet, so another agent can pick this up from the plan.
