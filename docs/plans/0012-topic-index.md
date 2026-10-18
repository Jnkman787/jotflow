# Topic index with a rolling window

## Links and status

- Issue: #12
- Also serves: #14 (`list_topics` returns this index), #37 (expiry reuses the window setting)
- Draft PR: #110
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18: 768 tests, 98% coverage, `orchestrator/topics.py` at 100%
- Next safe action: review; #14 returns `list_topics` from its MCP tool, and #37 reuses `topic_window_days`

## Objective and done-when

Every drafting run gets a compact index of the channel's active topics, so the agent can attach a new message to an existing topic instead of starting a duplicate. A topic is active while a message was linked to it within the window.

Done when:

- [x] `list_topics(channel)` returns only topics active within the window, most recently active first
- [x] Adding a message to a topic refreshes its activity (the store already does this; tested here through the index)
- [x] The index serialises to a compact form suitable for a prompt, with titles labelled as untrusted data
- [x] The window is configurable in `jotflow.toml` as `topic_window_days`
- [x] Topic titles are bounded and sanitised (#12 note): already enforced by the store on every write; the index cleans them again on the way out
- [x] `make check` passes, including the coverage floor

**Out of scope:** expiring topics whose issue closed, and reviving expired topics (#37). The MCP tool itself (#14).

## Design

- **Window setting.** `Policy.topic_window_days`, default 14, range 1 to 365, at the top level of `jotflow.toml`. The roadmap lists the window as a decision deferred to this milestone. 14 days covers a sprint-length discussion and keeps the index small. `policy_writer` writes it, and `jotflow.example.toml` documents it. #37 can reuse the setting for expiry.
- **Models** in `models.py`, so #14's `list_topics` can return them as its tool schema:
  - `TopicSummary`: id, title, repo, issue number, message count, up to five thread timestamps, and last activity.
  - `TopicIndex`: channel, window, generation time, the topics, how many more were cut by the limit, and a fixed note. The note says titles were written by earlier agent runs from Slack conversations and are untrusted data, never instructions (#14 notes).
- **Builder**, `orchestrator/topics.py`:
  - `build_index(store, channel_id, window_days=..., now=..., limit=50)` reads `Store.channel_topics(active_since=now - window)`. It cleans each title again with `clean_title`, as a second layer behind the store, and caps the list at `limit`.
  - `list_topics(store, policy, channel_id)` looks the window up in the policy and refuses a channel the policy does not list.
- **Compact form.** `TopicIndex.to_prompt()` is JSON with no spacing and no null fields. It is a labelled data object, never prose, so nothing in a title reads as part of the instructions.

## Risks and open questions

- The 14-day default is a judgment call. It is one setting to change if teams want more or less.
- `models.py` is also touched by #103 (`draft_cleared_at`); the two edits are in different classes.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `topic_window_days` in the policy, writer, and example
- [x] `TopicSummary` and `TopicIndex`
- [x] `build_index` and `list_topics`
- [x] Tests
- [x] Docs: architecture Correlation section, AGENTS.md layout
- [x] CodeRabbit once, or skipped: see Validation
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` green: 768 tests, 98% coverage. `tests/test_topic_index.py` runs against MemoryStore and SqliteStore. It covers only topics within the window, an inclusive window edge, a linked message bringing an old topic back, summary fields with the thread cap and order, other channels hidden, the 50-topic cap and the count of topics left out, a bad window or limit, the compact JSON form and its untrusted-data note, titles cleaned again even when a bad title reached the store some other way, `list_topics` taking the window from the policy and refusing an unconfigured channel, and `topic_window_days` defaulting, validating 1 to 365, and round-tripping through the writer.
- CodeRabbit: skipped, rate-limited.
- Docs: the architecture's Correlation section describes the index and the Configuration table lists the topic window. `jotflow.example.toml` documents `topic_window_days`. AGENTS.md's layout already lists the topic index under `orchestrator/`.

## Handoff notes

None yet.
