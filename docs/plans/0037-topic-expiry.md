# Topic expiry: close-out

## Links and status

- Issue: #37
- Status: ready for review
- Last verified: `make check`, 2026-10-19

## Objective and done-when

#12 already built topic expiry. This work proves the part an agent relies on, documents it, and drops the part JotFlow can't do.

- [x] **The expiry window is configurable.** It's `topic_window_days` in jotflow.toml, from #12, with tests in `tests/test_topic_index.py`.
- [x] **Expired topics can be revived if a new message is linked to them.** At the store level that's #12's `test_linking_a_message_brings_a_topic_back`. This PR adds the agent's path through the real tools: `get_topic` still reads an expired topic by id, and `link_message` with `existing_topic` and its id puts it back in the active index, in `tests/test_write_tools.py::test_an_expired_topic_is_revived_by_linking_to_it`.

**Dropped, 2026-10-19:** "expire when the linked issue is closed". JotFlow never holds GitHub credentials and can't see an issue's state (D5). Doing it would need the agent to report state, which isn't worth the trust it would take for an index-size optimisation. The quiet period already expires idle topics.

## Design

There's no code change. One test goes in `tests/test_write_tools.py`, and one sentence in architecture.md's topic lifecycle section.

## Validation

`make check` is green.
