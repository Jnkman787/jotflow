# Keep /jotflow help and /jotflow repos within Slack's limits

## Links and status

- Issue: #231
- Draft PR: #237
- Status: ready for review
- Last verified: `make check`, 2026-10-19
- Next safe action: review

## Objective and done-when

A channel has no repo limit, and a real deployment is about to configure 25 repos. Both replies must stay within what Slack accepts, however many repos a channel has.

Done when:

- [x] `/jotflow help` and `/jotflow repos` reply in `plain_text` section blocks, and no block exceeds 3,000 characters.
- [x] Help's repo line is capped, and ends "and N more; see /jotflow repos" when it's cut.
- [x] `/jotflow repos` lists every repo, split across blocks. Past Slack's limits, it ends with how many more repos `jotflow.toml` holds.
- [x] A test covers 25 or more repos at the maximum field lengths, for both replies.

**Out of scope:** the mode, link and other slash replies, whose text is short and fixed.

## Design

**The premise, corrected.** On main, both replies go out through `ack(text)`, as a message's `text`, not as blocks. Slack caps `text` at 40,000 characters, so 25 repos fit today. But `text` renders as mrkdwn, while help.py's docstring says everything it posts is `plain_text`, so nothing in it can ping or link. This change makes that true for both replies, and that is where the 3,000-character section limit comes in.

**`plain_sections(text)`** splits text into `plain_text` section blocks of at most 3,000 characters each, on line boundaries. A line that's longer on its own splits at the last space before the limit, or exactly at the limit if it has no space. `text` stays as the notification fallback.

**The budgets:**
- Help's "This channel feeds:" line holds at most 1,000 characters of repos. The rest becomes "and N more; see /jotflow repos".
- `/jotflow repos` holds at most 39,000 characters. That fits Slack's 40,000-character `text` cap and needs at most 13 blocks, well under Slack's 50 per message. Past that, it ends "and N more; jotflow.toml lists them all".

**The size of a full repo line:** a nickname of up to 32 characters, `owner/name` of up to 39 plus 100, and a description of up to 200, about 380 characters. So 25 repos make about 9,500 characters, and `/jotflow repos` sends them in four blocks.

## Risks and open questions

- Slack accepts `blocks` in an immediate slash command response, and Bolt's `ack(text=…, blocks=…)` sends them. The tests check the payload, not Slack itself.

## Progress checklist

- [x] Confirm the issue is ready
- [x] Commit this plan and open the draft PR
- [x] `plain_sections`, the two budgets, and the block replies
- [x] Tests with 25 or more maximum-length repos
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

`make check` passes. Measured with 25 repos at every field's maximum length:

| Reply | Characters | Blocks | Largest block |
| --- | --- | --- | --- |
| `/jotflow help` | 1,832 | 1 | 1,832 |
| `/jotflow repos` | 9,494 | 4 | 2,672 |

Before this change, help's single "This channel feeds:" line alone was 4,444 characters for the same channel. Tests cover help at 25 and 120 repos, `/jotflow repos` at 25 and 400, and a line too long for one section.

## Handoff notes
