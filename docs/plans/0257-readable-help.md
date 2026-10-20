# Readable /jotflow help and /jotflow repos

## Links and status

- Issue: #257
- PR: #259
- Status: ready for review
- Last verified: `make check`, 2026-10-20
- Next safe action: review by the reviewer and security

## Objective and done-when

A team found both replies hard to scan as one block of text, especially with 25 repos.

- [x] Help and repos render as headed, bulleted sections, with tests that snapshot the block structure.
- [x] Every admin-written field is escaped. A test puts `<!here>`, `<@U123>`, `https://x.y` and `*bold*` in a description, and shows no ping, no link, and no broken formatting.
- [x] The 25-repo and 120-repo maximum-length tests still pass within the block limits.
- [x] A CHANGELOG entry.

**Out of scope:** the pinned guide, the tip and the nudge. They're short, and stay `plain_text`.

## Design

`orchestrator/help_blocks.py` builds both replies. `help.py`'s text stays the plain `text` fallback.

- **Structure.**
  - Help is a header, then sections for Keywords, Reactions, Repos, Approval, and Commands, then an italic closing line. The Repos and Approval sections appear only for a configured channel.
  - Repos is one heading, then a bullet per repo: the nickname and `owner/name` in code, then the description.
- **No pings or links.**
  - Every mrkdwn object sets `verbatim: true`.
  - `escaped()` turns `&`, `<` and `>` into entities. It swaps `*`, `_`, `~` and the backtick for asterisk operator, modifier low macron, tilde operator, and modifier grave. Slack doesn't format any of those.
  - Nicknames and `owner/name` go in code spans, which their validators keep free of backticks. A value that ever had one would be escaped instead.
  - Our own headings use mrkdwn, and none of their text comes from the policy.
- **Limits.** `_pack` fills sections up to 3,000 characters, splitting only between lines, and every bullet is far shorter. Help keeps #231's 1,000-character repo budget, and its "and N more; see `/jotflow repos`" count matches the plain text. `/jotflow repos` shows as many repos as fit both 39,000 characters of plain text and 50 blocks, then "and N more; jotflow.toml lists them all."

## Validation

- `make check` passes.
- The test helpers check every block's type and length, `verbatim` on every mrkdwn object, and that no `<` or `>` is left in any mrkdwn.
- These tests cover 25 and 120 maximum-length repos for help, 25 and 400 for repos, the snapshot, the adversarial description, and code-span fallback.

## Handoff notes
