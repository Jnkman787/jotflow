# Fixture threads for tests and demo

## Links and status

- Issue: #18
- Also serves: #17, which will replay these fixtures
- Draft PR: #95
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18, 369 tests, 97% coverage
- Next safe action: review and merge #95; the replay-tests box on #18 completes with #17

## Objective and done-when

Give tests, the `jotflow replay` command (#17), and the README demo a set of saved Slack threads with a scripted fake-agent response and a documented expected outcome. The format mirrors the Slack event fields JotFlow actually reads, so a fixture exercises the real ingest path.

Done when:

- [x] A documented JSON format for a saved thread, covering:
  - the Slack event fields JotFlow reads: channel, ts, thread_ts, user, bot_id, text, subtype, and `reaction_added` events
  - the admin policy, the people in the thread, and topics that already exist
  - a scripted fake-agent section that the replay's fake agent answers with
  - the expected outcome
- [x] A loader with Pydantic validation, in `src/jotflow/testing/`, that rejects inconsistent fixtures with a message naming the fault
- [x] At least five fixtures, each documenting its expected outcome:
  - a clean feature request
  - a bug report with replies
  - pure chatter
  - two threads about the same topic
  - a message belonging to an existing topic
  - one with the keywords `idea:`, `re:`, and `skip:`
  - one with mentions of people
- [x] Every fixture runs through the real ingest filter in a test, and the accepted and ignored events match its expected outcome
- [x] A test asserts that no fixture contains real-looking Slack ids, organisation names outside the fictional set, or token-shaped strings
- [ ] Used by the replay tests. This box completes with #17, which does not exist yet. This PR uses the fixtures in the ingest and loader tests.

**Out of scope:** the replay command itself (#17), the store (#11), and keyword parsing (#19 era work). Fixtures record what keywords should do; nothing here implements them.

## Design

See Correlation, Conversation rules, and Intent keywords in [architecture.md](../architecture.md), and decision D4 in [roadmap.md](../roadmap.md).

**Where the files live.** The fixtures ship inside the package, under `src/jotflow/testing/threads/`, so `jotflow replay` can offer them as built-in demos after a PyPI install, not only from a clone. The loader is `src/jotflow/testing/threads.py`. It is standard library plus Pydantic, with no test-only dependency.

**Format, `jotflow-thread/1`.** One JSON object per fixture:

- `format`, `name`, `description`
- `workspace`: the fictional team and the bot's user id, bot id, and handle
- `users`: Slack user id to display name, optional GitHub login, and guest or external flags
- `policy`: the same structure as `jotflow.toml`, validated by `jotflow.policy.Policy`
- `existing_topics`: `jotflow.models.Topic` records that already exist before the thread starts
- `events`: Slack event payloads in delivery order, either `message` or `reaction_added`, limited to the fields JotFlow reads. Unknown fields are rejected, so fixtures stay readable.
- `agent`: the scripted fake agent, one entry per drafting run, each with the thread it drafts and the tool calls it makes. Arguments are validated by the same Pydantic models as the real MCP tools: `IssueProposal`, `UpdateProposal`, `Classification`. A `link_message` call that starts a new topic gives it a `topic_key`, and later calls refer to it as `key:<name>`, because real topic ids are assigned by JotFlow at replay time.
- `expected`: a one-line summary, the events ingest accepts and the ones it ignores with the reason, the topics with their messages, chatter, keywords, and proposals.

**Loader checks**, each failing with the field it is about:

- ts values are unique per channel, and `thread_ts` names an earlier top-level message in the same channel
- every user id an event or mention uses is listed in `users`, or is the bot
- agent calls refer only to messages in the fixture, `key:` topic ids are declared before use, and existing topic ids exist
- proposals target a repo the channel lists, with labels from its allowlist, using the same policy checks as #29
- `expected` is consistent: every accepted message is in exactly one topic, in chatter, or in a skipped thread, and the agent script produces exactly the expected topics and proposals

**Fictional data guard.** A test scans every fixture file. Slack ids must follow the fictional pattern: a type letter, `0`, capital letters, and an optional final digit, such as `C0EXAMPLE1` or `U0ALICE`. Real Slack ids mix digits into the rest. GitHub owners must be `acme-co`, the team is Acme Co, and URL and email domains must come from a short allowlist: `example.com`, `github.com/acme-co`, and the like. Strings shaped like Slack or GitHub tokens fail the test. The test uses an allowlist, not a list of forbidden names, so no real organisation is named in the repository even as a deny-list entry.

Alternatives considered: fixtures under `tests/` would not ship with the package, so the README demo would need a clone. YAML would need a new dependency. Storing raw Slack exports would carry many fields JotFlow never reads and invite real identifiers.

## Risks and open questions

- #17 may need fields this format lacks. The `format` version lets it extend the format without breaking these files.
- Keyword behaviour is documented in `expected.keywords` before the parser exists. If the parser's decisions differ, the fixture is the spec and the difference is discussed on the issue.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Loader and models in `src/jotflow/testing/threads.py`
- [x] Seven fixtures and a README describing the format and each fixture's outcome
- [x] Tests: loader validation, ingest against every fixture, scripted outcome, fictional data guard
- [x] Update documentation: AGENTS.md layout
- [x] `make check` passes, coverage at or above the floor
- [x] CodeRabbit local review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make check` passes: ruff, mypy strict, and 369 tests with 97% total line and branch coverage. `src/jotflow/testing/threads.py` is at 99%.
- `tests/test_fixture_threads.py` adds 68 tests:
  - every built-in fixture loads, and the seven required ones ship
  - each fixture's events go through the real `EventFilter`, and the accepted and ignored events match `expected` exactly, including the edit, the bot's own message and reaction, the channel join, and the redelivery
  - the script's topics, chatter, and proposals match `expected`
  - keyword entries match the message text, and skipped messages reach no agent run
  - the fictional-data guard passes on every file, and catches real-looking ids, foreign URLs and email domains, and token-shaped strings in samples
  - every loader check fails with its own message on a mutated fixture
- `uv build --wheel` includes `jotflow/testing/threads/*.json` and the README, so the fixtures ship with the package.
- `make eol` passes, and every new file is LF.
- CodeRabbit (local CLI, `--committed --base main -c AGENTS.md`): 0 findings across all 13 files.

## Handoff notes

- #17 can load fixtures with `builtin_thread(name)` or `load_thread(path)`, feed `thread.events` through ingest, answer tool calls from `thread.agent`, map `key:<name>` topic ids to the ids it assigns, and compare with `thread.expected` and `scripted_outcome(thread)`.
- The fixtures were generated once by a throwaway script and are now edited by hand. The JSON files are the source of truth.
