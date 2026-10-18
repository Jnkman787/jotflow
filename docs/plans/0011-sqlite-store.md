# SQLite store

## Links and status

- Issue: #11
- Also serves: #53 (run records build on this schema), #12 (topic titles are bounded and sanitised here)
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: open the draft PR, then write `src/jotflow/store/`

## Objective and done-when

JotFlow's local state lives in a store behind a small async repository interface: aiosqlite for real use, so Postgres stays possible behind the same interface, and an in-memory implementation for tests. It stores pointers and derived state only, never a Slack message body (Data and privacy in [architecture.md](../architecture.md)).

Done when:

- [ ] Schema with versioned migrations for message refs, topics, proposals and their state, identity links, and channel modes
- [ ] Proposals keep the content hash and approver (D7) and the Slack users whose messages they came from
- [ ] No column can hold a message body: a test reads the live schema and fails if any column is named or documented as a message's body, text, or content
- [ ] Proposal state changes follow the architecture diagram and are atomic: a change from an unexpected state fails and changes nothing
- [ ] Topic titles are bounded and sanitised on every write: length limit, no markup, no links, no mentions (#12 note)
- [ ] A repository interface with SQLite and in-memory implementations, both passing one shared contract test suite
- [ ] `make check` passes, including the coverage floor

**Out of scope:** run records and their table (#53, the architect, as migration 2). The rolling topic window and the compact prompt form (#12); the store offers the activity timestamps they need. Wiring the store into `jotflow run`.

## Design

Package `src/jotflow/store/`:

- `base.py`: the `Store` protocol and its errors. Every method is async.
  - Message refs: `link_message(ref, topic_id, by)`, `mark_chatter(ref, by)`, `get_message(channel_id, ts)`, `topic_messages(topic_id)`. `by` is `agent` or `person`, so a person's override is recorded and an agent can't silently undo it.
  - Topics: `create_topic(channel_id, title)`, `get_topic(id)`, `rename_topic(id, title)`, `set_topic_issue(id, repo, number)`, `channel_topics(channel_id, active_since=None)`. Each linked message refreshes the topic's last activity.
  - Proposals: `add_proposal(record)`, `get_proposal(id)`, `transition(id, expected, to, ...)`, `supersede_pending(channel_id, thread_ts, keep_id)`, `thread_proposals(channel_id, thread_ts)`.
  - Identity: `link_identity(slack_user_id, github_login)`, `get_identity(slack_user_id)`, `unlink_identity(slack_user_id)`. Links are self-declared and stored as unverified in v1. A GitHub login can be claimed by one Slack user at a time.
  - Channel modes: `set_channel_mode(channel_id, mode, changed_by)`, `get_channel_mode(channel_id)`.
- `migrations.py`: an ordered list of `(version, statements)`, tracked with `PRAGMA user_version` and applied one transaction per version. Every column carries a `--` comment saying what it holds. SQLite keeps these comments in `sqlite_master`, so the schema test can read them.
- `sqlite.py`: `SqliteStore.open(path, clock=...)`. Foreign keys are on, WAL journal mode, and a busy timeout. Transitions are one conditional `UPDATE ... WHERE id = ? AND state IN (...)` inside `BEGIN IMMEDIATE`. If no row changes, nothing is written and the store raises.
- `memory.py`: `MemoryStore`, with the same semantics, behind an `asyncio.Lock`.
- `titles.py`: `clean_title(text)`. It removes Slack mentions and links, URLs, markup characters, and control characters, collapses whitespace, and cuts to 80 characters on a word boundary. An empty result is an error.
- `hashing.py`: `content_hash(proposal)`, a SHA-256 over canonical JSON of the agent's structured proposal. Approving stores it. A caller may pass the hash the card showed, and approval fails if the content has changed since.

Proposal transitions, as in the architecture diagram:

| From | To |
|---|---|
| pending | approved, rejected, superseded |
| approved | executing |
| executing | applied, reconciling, failed |
| reconciling | applied, approved, failed |
| failed | approved |

Approval records `approved_by` (a Slack user id, or `auto`), `approved_at`, and the content hash. `applied` needs a result with an issue URL; `failed` may carry an error result.

Models: `ProposalRecord` gains `participants`, the Slack user ids whose messages a proposal came from, for the D7 rule that no one approves their own proposal when approvers are on. Assignees stay inside the agent's structured proposal.

What the proposals table holds: the agent's structured proposal, including the drafted issue title and body, as JSON. That is the derived content the architecture lists ("proposals: structured fields"), which a person approves and which ends up in a GitHub issue. It is not a Slack message. The column is named and commented as the agent's draft, and the schema test checks that no column claims to hold a message.

Runs: `proposals.run_id` is text with no foreign key, so a proposal does not need a run row until #53 adds the `runs` table as migration 2.

## Risks and open questions

- The drafted issue body can quote what people wrote in Slack. It is stored because approval must lock exactly what the card showed (D7). Rebuilding the store from Slack does not need it.
- SQLite cannot add a foreign key with `ALTER TABLE`, so a later foreign key from proposals to runs means rebuilding the table. I've asked the architect whether to keep `run_id` unenforced.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Migrations and the schema comments
- [ ] Protocol, errors, `clean_title`, `content_hash`
- [ ] `SqliteStore`
- [ ] `MemoryStore`
- [ ] Shared contract tests, schema test, title tests, concurrency test for transitions
- [ ] Docs: architecture Data and privacy, AGENTS.md layout
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
