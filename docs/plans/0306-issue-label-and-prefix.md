# A label and a title prefix on every issue from Slack

## Links and status

- Issue: #306
- PR: to be opened
- Status: ready for review
- Last verified: 2026-10-29, `make check` on the branch
- Next safe action: review

## Objective and done-when

Teams want issues from Slack to be easy to filter. This adds `[issues] label`, a label on every new issue, and `[issues] title_prefix`, text before every new title. Both are off by default.

- [x] `[issues] label` adds the label to every create proposal, and it can't be removed on the card
- [x] `[issues] title_prefix` is off by default, and when set it's applied after `title_pattern` validation, documented as conflicting with strict title patterns
- [x] Tests and docs

**Out of scope:** labels or prefixes on updates, per-channel settings, and JotFlow creating the label.

## Design

- **Policy:** `IssuesPolicy` in `policy.py`, as `Policy.issues`.
  - `label` uses the existing label rule.
  - `title_prefix` is 1 to 32 characters of plain text: no surrounding space, no control characters, and no `<`, `>`, `@` or backtick, so it can't be markup or a mention.
  - `filed_title()` adds the prefix and a space, stays within GitHub's 256 characters, and never adds it twice. `filed_labels()` adds the label once, compared without case. `bare_title()` takes the prefix back off.
- **Applied when the issue is written,** in `execution/writes.plan()`, for new issues only. The draft itself is unchanged.
  - So the `title_pattern` check, which runs on the draft at `propose_issue`, sees the title without the prefix.
  - No card edit can take either off. A repo move keeps them, because they aren't in the draft.
  - The approval hash is unchanged, because the settings are the admin's, not the draft's.
- **The label is checked like the draft's own.** It's in `checks.labels`, so the execution run confirms it exists with `get_label`, and writes nothing if it's missing. JotFlow never creates a label.
- **The card shows what's filed.** It shows the prefixed title, and the labels line ends with the label, marked "(added to every issue)". `CardContext.issues` carries the setting.
- **Setup keeps `[issues]`** when it rewrites `jotflow.toml`, and writes nothing for a default.
- **`jotflow rebuild --from-github`** restores topic titles without the prefix.
- **`jotflow check`** prints a note for each setting:
  - with a label, that it must exist in every repo, and what happens otherwise
  - with a prefix and any repo `title_pattern`, which filed titles won't match the pattern

## Risks and open questions

1. **Deviation from the issue:** the issue says `jotflow check` "warns if [the label] doesn't" exist. `jotflow check` runs on the host and can't reach GitHub, since only the runners can, and it has always been offline. So it notes the requirement instead of checking each repo. The write still fails closed, naming the missing label. A startup check through the drafting runner could follow if teams want it.

## Validation

- `make check` passes.
- `tests/test_issues_policy.py` covers:
  - off by default
  - the prefix and label in the planned write and its label check
  - no doubling, and the 256-character limit
  - nothing added to updates
  - the card's title and label line
  - refused settings
  - setup's round trip, `bare_title`, and check's notes

## Handoff notes

None.
