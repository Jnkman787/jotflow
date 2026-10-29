# Follow-ups revise a pending proposal, and become updates once filed

## Links and status

- Issue: #304
- Draft PR: #310
- Status: ready for review. Approved by the lead, the reviewer and security, with the conditions below built in
- Last verified: origin/main b3afbf3, 2026-10-29
- Next safe action: build. Migration 16 is this PR's, as the lead confirmed

## Objective and done-when

A team using JotFlow found that ideas added to a thread while its card waits for an approver are lost. drafting.md step 6 says a topic with a pending proposal gets no new proposal, so later replies are linked to the topic but never reach the card.

From #304:

- [x] A reply to a pending proposal's thread revises that proposal's draft, and the card shows the revision (replay fixture).
- [x] A reply after the issue is filed yields an update proposal, defaulting to a comment (replay fixture).
- [x] A top-level follow-up about a filed issue is linked and yields an update, or a question when unclear (replay fixture).
- [x] Approval still locks exactly what the card shows.

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
    revised_at TEXT NOT NULL -- UTC, ISO 8601
)
CREATE TABLE proposal_revision_authors (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    user_id TEXT NOT NULL -- Slack user id whose message the revision added
)
ALTER TABLE proposals ADD COLUMN follow_up_due_at TEXT
    /* UTC: a revision lost a race to an approval; draft the thread again once filed */
```

Authors are one row per user, as `proposal_participants` are, so any path that finds or removes a Slack user's rows finds these too (the reviewer's nit). Today no path erases a Slack user's proposal rows. Draft clearing removes text only, and these hold no text, so the rows go with the proposal, by the cascade, and the threat model's data table lists them.

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
- **Caps:** revisions have their own cap, 3 a run, separate from the proposal cap of 3, so a busy run that revises one topic can still propose for others (the reviewer's nit). There are at most 10 revisions a proposal; past that, the agent is told to ask instead.
- **Doc passages (#209):** the revised text runs the same long-passage check as `propose_*`. A flag from an earlier revision stays (security's condition 2).
- **Losing a race (the reviewer's must-address 2):**
  - **The proposal is still pending, but `base_revision` is stale,** because a person edited it or another run revised it. The tool doesn't fail. It answers `{"revised": false, "revision": N, "draft": …}`, the current revision and draft. The agent retries once, in the same run, on top of them. The draft is the one the card already shows in this channel.
  - **The proposal was approved, rejected or applied first.** The store sets `follow_up_due_at` on it. The card loop arms the thread once the proposal is applied, rejected, superseded, or failed and then discarded (the reviewer's build note), so the next quiet window drafts the follow-up. After a rejection, the packet marks the topic `rejected`, so the re-draft proposes again only if the request changed.
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
- **Each changed field is marked on the card,** and the revision line ends "Reread before approving". The marks come from `proposal_revisions`, not the draft, so they sit outside the hash. They only point at what to reread. The previous title isn't shown: keeping old text outside the draft would escape draft clearing, and the lead accepted dropping it.
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

### Reactions on a changed card (decided: C)

A reaction names only the card's message, not what it showed, so a reaction made while reading one draft could approve a later one. #304 lets anyone in the thread cause a revision, so that becomes reachable by anyone.

**The decision.** Once a pending card has changed in place, only its hash-carrying button approves it. That covers a revision from new messages or a person's card edit, such as repo, assign or unassign, as the reviewer asked: they redraw in place too. A reaction then gets a private reply to use the button. A never-changed card approves by reaction as before.

There's no clock comparison and no second card message. The store's own records of revisions and edits say whether a card has changed. The lead chose this, and security and the reviewer agreed. A fresh card per revision (option A) and comparing `event_ts` (option B) were set aside.

### Approval: what's tested

- A Approve click carrying the pre-revision hash is refused and the card redrawn.
- A reaction while the card is behind is refused.
- Approving the redrawn card locks the revised draft's hash.
- A revision built on a stale `base_revision` changes nothing, and answers with the current draft.
- Under `require_second_approver`, someone whose message a revision added can't approve it. Source authors are re-read at approval, so this needs no stored state, and nothing is stored as a partial approval today (security's condition 1).
- After any redraw, a reaction is refused with a private reply, including Slack's retry of a reaction made before it. A never-redrawn card approves by reaction as before.

## Threat model (security's condition 3)

A new row: anyone posting in a thread can change a pending draft. It's bounded by:
- the card naming who changed it, and what;
- the reaction rule above, and the hash-carrying button;
- `base_revision` refusing a stale revision;
- the caps.

## Risks and open questions

1. **The migration number.** 16 is the next free number on main today. Another open PR could claim it first.
2. **Revision churn.** Every new message in a busy thread could revise the card. The rule against revising for a +1, the per-run cap and the 10-revision cap bound it. A revision also needs the next quiet window, so the card doesn't flicker mid-conversation.
3. **A person's edit racing a revision.** `base_revision` makes the store refuse a stale revision, and the agent then skips it rather than overwriting the person's edit. The packet's next run sees both.
4. **Packet size:** the packet would carry a pending draft's body whole, since the revision replaces it and a cut-short body would lose text. That's bounded only by the 64 KiB MCP request cap that limited it when it was proposed. With several pending proposals in one thread, that adds up, so the packet carries `pending` only for the topics this thread feeds.

## Progress checklist

- [x] Confirm the issue is ready
- [x] Commit this plan and open the draft PR
- [x] The lead and the reviewer review this plan, and product confirms the migration number
- [x] Store: migration 16, `revise_proposal`, `proposal_revisions`, both stores
- [x] The `revise_proposal` tool and the packet's `pending`
- [x] drafting.md step 6
- [x] The card: the revision line and the update header
- [x] Replay: `sees_until`, proposal keys, and the revision expectations
- [x] The five fixtures, and the approval tests
- [x] Docs: usage, architecture, the threat model, and the CHANGELOG
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` is green.
- `jotflow replay` and `--agent scripted` match for the four new fixtures. The plan's fifth case, a top-level follow-up about a filed issue, is the existing `existing-topic.json`.
- The deploying team's store, counts only, confirmed both halves of the bug. Every pending proposal was still at revision 1, and a reply after filing got no update card. `follow-up-filed-reply.json` reproduces the second: a linked reply after filing yields `propose_update`.

## Handoff notes

All fixtures stay fictional: Acme Co, `C0EXAMPLE1`, `acme-co`.
