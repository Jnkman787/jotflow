# Replay checks keywords with the real parser

## Links and status

- Issue: #129
- Also serves: #17, #20, #56
- Draft PR: #132
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18
- Next safe action: review; when #56 resolves `re:` handles, drop the topic comparison's set-aside for them

## Objective and done-when

`jotflow replay` reported keyword expectations as not run because no parser existed. #125 added `orchestrator/keywords.py` and the live work packet, so replay now uses them, with no second parser.

Done when:

- [x] Replay checks each fixture's keyword expectations with `split_keyword`, and none is reported as not run
- [x] The keywords and existing-topic fixtures pass, and a test shows a wrong keyword expectation fails the replay
- [x] When #56 extends the module, replay picks the extension up with no change
- [x] `make check` passes, including the coverage floor

**Out of scope:** resolving `re: web#42` to a topic, which #56 adds to the keyword module. Until then the topic comparison sets messages attached by `re:` aside, and says so in its detail.

## Design

- Replace replay's hand-written `_FixtureSource` with the real `SlackWorkSource` (#125), fed by a `_FixtureReader`. The reader answers `replies` with a thread's accepted messages, parent first, in Slack's `conversations.replies` shape, and `user` with the fixture user's display name in the `users.info` shape. So replay gets exactly the live packet's rules: `skip:` messages and threads left out, keywords stripped, the keyword hint, mentions resolved, and related excerpts.
- The `keywords` check compares `split_keyword` on each accepted message against `expected.keywords`. It runs for every fixture, and is never reported as not run.
- Both pieces call the keyword module's own functions, so #56's extensions reach replay unchanged.

## Risks and open questions

- `re:` attachment is still #56's; see Out of scope.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `_FixtureReader` and the switch to `SlackWorkSource`
- [x] The real keyword check
- [x] Tests
- [x] CodeRabbit once, or skipped: see Validation
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- Every built-in fixture still replays to its expected outcome, now through the real `SlackWorkSource`, and the `keywords` check runs and passes for all eight. No check is reported as not run.
- New tests:
  - a wrong keyword expectation, labelling "Skip button is broken" as `bug`, fails the replay
  - the real work packet refuses a `skip:` thread and strips `idea:` into a hint
  - the fixture reader returns the parent first, honours `limit`, and gives display names in Slack's shapes
- The old fixture source and the "not run" path are removed, with their tests.
- CodeRabbit: skipped, rate-limited.

## Handoff notes

None yet.
