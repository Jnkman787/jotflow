# Follow-ups revise a pending proposal, and become updates once filed

## Links and status

- Issue: #304
- Draft PR: #310
- Status: planning. The store changes, so the plan comes first, as the lead asked
- Last verified: origin/main b3afbf3, 2026-10-29
- Next safe action: the lead and the reviewer review this plan, and product confirms the migration number

## Objective and done-when

A team using JotFlow found that ideas added to a thread while its card waits for an approver are lost. drafting.md step 6 says a topic with a pending proposal gets no new proposal, so later replies are linked to the topic but never reach the card.

From #304:

- [ ] A reply to a pending proposal's thread revises that proposal's draft, and the card shows the revision (replay fixture).
- [ ] A reply after the issue is filed yields an update proposal, defaulting to a comment (replay fixture).
- [ ] A top-level follow-up about a filed issue is linked and yields an update, or a question when unclear (replay fixture).
- [ ] Approval still locks exactly what the card shows.

**Out of scope:**

- Revising a failed proposal. Retry approves the draft that failed, and a revision would change it under the person retrying.
- Changing the repo by revision. The repo is the person's choice, through the card's dropdown (#160).

## What exists, and what's missing

- **Locking already holds.** The Approve button carries the hash of the draft the card shows, and a click whose hash is stale is refused and the card redrawn (D7). A reaction is refused while the card is behind its proposal's revision (`card_due`). A revision raises the revision, so both refuse a click or reaction on the old card. The plan adds tests for that, and no new mechanism.
- **The store can replace a pending draft,** but only as a person's card edit. `proposal_edits.field` has a CHECK constraint, `IN ('repo', 'assign', 'unassign')`, and records one Slack user. A revision from new messages changes other fields, and comes from the people who wrote those messages, so it needs its own record.
- **Updates after filing exist.** `propose_update` takes a comment, body or label changes, and the card already shows "Comment:" or the new body. What's missing is guidance and fixtures.
- **Top-level follow-ups:** classifying a message as `existing_topic` already links it. What's missing is guidance to look for it, and to ask when unsure, plus a fixture.

## Design

### Store: migration 16

A new table, with pointers and Slack ids only, never the values:

```
CREATE TABLE proposal_revisions (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, -- the proposal's revision this one made
    run_id TEXT NOT NULL, -- the drafting run that revised it
    changed TEXT NOT NULL, -- which fields, from a fixed set, comma-separated
    added_by TEXT NOT NULL, -- Slack user ids whose messages it adds, space-separated
    revised_at TEXT NOT NULL -- UTC, ISO 8601
)
```

**`Store.revise_proposal(proposal_id, draft, *, run_id, base_revision, added_by)`** works like this:

- It applies only to a pending proposal, and only when its revision still equals `base_revision`. Otherwise it raises `TransitionError`, so a person's card edit or approval that landed after the run read the draft is never overwritten.
- The repo, kind, topic and issue number stay. The draft's sources and participants merge with the old ones.
- It computes `changed` itself, by comparing old and new, from `title`, `body`, `labels`, `assignees`, `comment`, `body_change` and `label_changes`. It refuses a revision that changes nothing.
- It raises `revision`, so the card is redrawn and the old hash stops approving.

`proposal_revisions(proposal_id)` returns the revisions, oldest first. Both the memory and SQLite stores implement it. Migration 16 is the next free number on main. Product, which owns migration numbering, confirms before code.

### The tool: `revise_proposal`, drafting runs only

- **Arguments:** `proposal_id`, `base_revision`, the whole revised draft (`title`, `body`, `labels` and `assignees` for a new issue, or `changes` for an update), the new `sources`, and `confidence`.
- **Checks:** the same as `propose_issue` and `propose_update`, by the same code. That covers labels from the repo's list, `title_pattern`, @-mentions, links, the issue-links setting, and assignees from linked participants. Every new source must be in this run's packet.
- **Proposals:** only one in this channel, pending, whose topic is one the run's thread feeds.
- **Caps:** it counts toward the run's proposal cap of 3. There are at most 10 revisions a proposal; past that, the agent is told to ask instead.
- **`added_by`** is the authors of the new sources, read through the same Slack lookup the approval gate uses for source authors, so the agent can't name them.

### The packet

A topic in `topics` with a pending proposal now carries `pending`. That holds:

- `proposal_id`, `revision` and `kind`;
- the draft's title, body, labels and assignees, or its `changes`.

The text is the agent's own earlier draft, built from Slack text, so it is marked untrusted, like Slack text. A topic with a filed issue keeps `issue_number`, as now.

### drafting.md step 6

- **A pending proposal:** call `revise_proposal` with the whole revised draft, folding in what the new messages add. Keep everything they don't change. Don't revise for a +1, thanks or discussion that adds nothing.
- **A filed issue:** call `propose_update`. By default, that's a `comment` summarising the new information. Change the body only when the request itself changes, such as its scope or acceptance.
- **A top-level message** that clearly continues a filed or pending topic gets `existing_topic` with that topic, then the step above. When it isn't clear which topic it continues, ask (step 8).

The instructions cap is 9,500 bytes, and the file is 9,208. I'll make room by tightening steps 5 and 6. If that isn't enough, I'll ask for the cap to rise to 10,000.

### The card

- **A revised card** gets a context line under its header: "Revised with new messages from Alice and Bob: body, labels." The names are display names, escaped. The field list is JotFlow's, never the agent's.
- **An update card's header** says what it is: "Proposed comment on web#42", or "Proposed change to web#42's body" when the body changes.

### Replay

- **What a run sees:** `AgentRun` gains `sees_until`, a message ts. A run's packet holds only that message and the ones before it, so a fixture can play the first draft and then its revision.
- **Linking scripted calls:** `ProposeIssueCall` gains `proposal_key`. A new `ReviseProposalCall` names `key:<name>`, and replay fills in the real id and revision.
- **Expected:** `ExpectedProposal` gains `revisions`, the count; `revised_by`, the Slack ids; and `changed`, the fields of the last revision.
- **New fixtures:**
  - `follow-up-pending`: a reply while pending revises the draft, the card names who added it, and an old-hash approval is refused.
  - `follow-up-filed-comment`: a reply after filing yields a comment update.
  - `follow-up-filed-scope`: a reply that changes the scope yields a body change.
  - `follow-up-top-level`: a new post about a filed issue is linked and yields an update.
  - `follow-up-unclear`: a new post that could continue either of two topics gets a question.

### Approval: what's tested

- A Approve click carrying the pre-revision hash is refused and the card redrawn.
- A reaction while the card is behind is refused.
- Approving the redrawn card locks the revised draft's hash.
- A revision built on a stale `base_revision` changes nothing.

## Risks and open questions

1. **The migration number.** 16 is the next free number on main today. Another open PR could claim it first.
2. **Revision churn.** Every new message in a busy thread could revise the card. The rule against revising for a +1, the per-run cap and the 10-revision cap bound it. A revision also needs the next quiet window, so the card doesn't flicker mid-conversation.
3. **A person's edit racing a revision.** `base_revision` makes the store refuse a stale revision, and the agent then skips it rather than overwriting the person's edit. The packet's next run sees both.
4. **Packet size:** the packet would carry a pending draft's body whole, since the revision replaces it and a cut-short body would lose text. That's bounded only by the 64 KiB MCP request cap that limited it when it was proposed. With several pending proposals in one thread, that adds up, so the packet carries `pending` only for the topics this thread feeds.

## Progress checklist

- [x] Confirm the issue is ready
- [x] Commit this plan and open the draft PR
- [ ] The lead and the reviewer review this plan, and product confirms the migration number
- [ ] Store: migration 16, `revise_proposal`, `proposal_revisions`, both stores
- [ ] The `revise_proposal` tool and the packet's `pending`
- [ ] drafting.md step 6
- [ ] The card: the revision line and the update header
- [ ] Replay: `sees_until`, proposal keys, and the revision expectations
- [ ] The five fixtures, and the approval tests
- [ ] Docs: usage, architecture, the threat model, and the CHANGELOG
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

None yet.

## Handoff notes

All fixtures stay fictional: Acme Co, `C0EXAMPLE1`, `acme-co`.
