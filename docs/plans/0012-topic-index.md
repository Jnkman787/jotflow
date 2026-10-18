# Topic index with a rolling window

## Links and status

- Issue: #12
- Also serves: #14 (`list_topics` returns this index), #37 (expiry reuses the window setting)
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: add `topic_window_days` to the policy, then the index models and builder

## Objective and done-when

Every drafting run gets a compact index of the channel's active topics, so the agent can attach a new message to an existing topic instead of starting a duplicate. A topic is active while a message was linked to it within the window.

Done when:

- [ ] `list_topics(channel)` returns only topics active within the window, most recently active first
- [ ] Adding a message to a topic refreshes its activity (the store already does this; tested here through the index)
- [ ] The index serialises to a compact form suitable for a prompt, with titles labelled as untrusted data
- [ ] The window is configurable in `jotflow.toml` as `topic_window_days`
- [ ] Topic titles are bounded and sanitised (#12 note): already enforced by the store on every write; the index cleans them again on the way out
- [ ] `make check` passes, including the coverage floor

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
- [ ] Commit this plan and open the draft PR
- [ ] `topic_window_days` in the policy, writer, and example
- [ ] `TopicSummary` and `TopicIndex`
- [ ] `build_index` and `list_topics`
- [ ] Tests
- [ ] Docs: architecture Correlation section, AGENTS.md layout
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
