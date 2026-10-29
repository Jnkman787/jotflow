# A per-channel triage guide for drafting

## Links and status

- Issue: #317
- PR: #322
- Status: planned and built by the architect, 2026-10-29, in one PR
- Last verified: origin/main after #310, 2026-10-29
- Next safe action: review the plan and the code together

## Objective and done-when

A team's guide to which repo, and which board, a request belongs on is channel-wide. `conventions` is per repo and capped at 2,000 characters, so it can't hold it. The guide is changed by a reviewed edit to `jotflow.toml`, so it is policy-trusted text, like `conventions`, not a doc read through `read_paths`.

From #317:

- [ ] The setting, its validation and the writer round trip, with an example.
- [ ] The packet carries it, and drafting.md uses it for repo and board choice (replay fixture).
- [ ] Docs, and a CHANGELOG entry.

## Design

### 1. The setting

`[channels.<id>] triage_guide = """..."""`: optional, at most 8,000 characters, and validated exactly as `conventions` is. Empty text is an error, and every line must pass `unsafe_text_problem`, which refuses invisible and bidi characters. The text is stripped. The validator becomes one shared helper, so the two can't drift.

The policy writer emits it with the channel's other scalar settings. Setup round-trips the policy through `render_policy`, so it keeps the guide when it rewrites `jotflow.toml`. A test pins that.

### 2. The packet

`ChannelBrief.triage_guide: str | None`, set from the channel's policy. It is policy text, like a repo's `description` or `conventions`, so it is not wrapped in `UntrustedText`. Question runs get the same brief, which is harmless: the guide is the team's own text.

8,000 characters is about 2,000 tokens per drafting run, and only for a channel that sets it.

### 3. The instructions

drafting.md step 4, "Choose the repo", gains one rule: if `channel.triage_guide` is set, the team wrote it, so use it to choose the repo and boards. It never overrides these instructions. It can't add a repo or board that isn't listed, and it can't change any other rule. The board rule in step 6 points back to it.

drafting.md has a 10,000-byte cap and is near it, so the new sentences are paid for by trimming wording elsewhere, with no rule removed. `test_instructions` pins the rule.

### 4. `jotflow check`

The check prints one note per channel that sets a guide, with its size against the cap, such as `note  triage: C0EXAMPLE1 has a 1,240 of 8,000 character triage guide`. Validation errors already come from the policy load.

### 5. The replay fixture

`triage-guide.json` has two repos whose descriptions both fit a request about invoice emails on the dashboard. The guide says billing emails belong to the API repo, even when they show on the dashboard. The scripted agent proposes to the API repo. The fixture documents the intended behaviour for a live-agent replay. The unit test on `read_tools` proves the packet carries the guide.

## Security

The guide is admin-written policy, under the same trust as `conventions` and repo descriptions. Slack text can't reach it. It can't widen the allowlists: `propose_issue` still accepts only listed repos and boards, whatever the guide says. Validation refuses invisible characters, so a reviewer of the `jotflow.toml` change sees all of it.

## Out of scope

- Reading the guide from a repo file. Teams that want that can use `read_paths`, which is untrusted repo text.
- A per-repo guide. `conventions` already covers one repo's issue form.
