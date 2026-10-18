# Replay checks keywords with the real parser

## Links and status

- Issue: #129
- Also serves: #17, #20, #56
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: replace replay's fixture source with the real `SlackWorkSource`

## Objective and done-when

`jotflow replay` reported keyword expectations as not run because no parser existed. #125 added `orchestrator/keywords.py` and the live work packet, so replay now uses them, with no second parser.

Done when:

- [ ] Replay checks each fixture's keyword expectations with `split_keyword`, and none is reported as not run
- [ ] The keywords and existing-topic fixtures pass, and a test shows a wrong keyword expectation fails the replay
- [ ] When #56 extends the module, replay picks the extension up with no change
- [ ] `make check` passes, including the coverage floor

**Out of scope:** resolving `re: web#42` to a topic, which #56 adds to the keyword module. Until then the topic comparison sets messages attached by `re:` aside, and says so in its detail.

## Design

- Replace replay's hand-written `_FixtureSource` with the real `SlackWorkSource` (#125), fed by a `_FixtureReader`. The reader answers `replies` with a thread's accepted messages, parent first, in Slack's `conversations.replies` shape, and `user` with the fixture user's display name in the `users.info` shape. So replay gets exactly the live packet's rules: `skip:` messages and threads left out, keywords stripped, the keyword hint, mentions resolved, and related excerpts.
- The `keywords` check compares `split_keyword` on each accepted message against `expected.keywords`. It runs for every fixture, and is never reported as not run.
- Both pieces call the keyword module's own functions, so #56's extensions reach replay unchanged.

## Risks and open questions

- `re:` attachment is still #56's; see Out of scope.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] `_FixtureReader` and the switch to `SlackWorkSource`
- [ ] The real keyword check
- [ ] Tests
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
