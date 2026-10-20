# Related issues on the card, and "same as"

## Links and status

- Issue: #36 (M6: After v0.1.0)
- Draft PR: to be opened
- Status: built, ready for review
- Last verified: 2026-10-20, against main
- Next safe action: review the open questions below

## Objective and done-when

The drafting agent already searches for duplicates (drafting.md section 5). When it finds an issue that looks like the *same* idea, it asks in the thread instead of proposing. When it finds issues that are only *related*, it proposes, and an approver never sees what it found. A duplicate it misjudged as related then reaches GitHub.

Done when:

- [ ] `propose_issue` and `propose_update` accept an optional `related` list: up to 3 issue references the agent found, each `owner/name#N` plus `open` or `closed`
- [ ] The card shows them in one context line, such as `Related: acme-co/web#42 (agent says closed) · acme-co/web#57 (agent says open)`, each linked to its GitHub issue by JotFlow. The state is labelled as the agent's claim, since JotFlow can't check it
- [ ] The card offers **Same as acme-co/web#42** for the first candidate on a pending create proposal. A person allowed to approve can click it. JotFlow then:
  - links the proposal's topic to that issue
  - closes the proposal without writing anything to GitHub
  - edits the card in the thread to say "Same as" the existing issue, with its link, which is how the thread hears of it
  - shows on the card who marked it, and as the same as what
- [ ] Nothing new is stored but pointers: repo, number, the agent's claimed state, and the Slack id of who clicked. No issue title or body ever reaches the card or the store
- [ ] **"Not a duplicate" undoes a wrong "same as",** under the same rules. It unbinds the topic only if the topic still points at that issue and no proposal has been applied to the topic since. Then it queues a fresh drafting run for the thread
- [ ] A threat-model row: "Suppress a real issue by steering a wrong 'same as'"
- [ ] The drafting instructions say when to fill `related`, and when to ask instead
- [ ] Tests cover the validation, the card, the click's gate checks, and the store transition. `make check` passes

## Design

- **The model.** Add `RelatedIssue(repo: RepoName, number: int >= 1, state: Literal["open", "closed"])`, plus `related: list[RelatedIssue]`, max 3, default empty, on `IssueProposal` and `UpdateProposal`. The list is part of the draft's canonical JSON, so the content hash covers it, and an edit or a stale click is caught as today.
- **Validation in `propose_issue` and `propose_update`.** Each `repo` must be one of the channel's configured repos, compared case-insensitively. Anything else is dropped with a warning in the tool result, not refused, so a stray public search hit doesn't cost the whole draft. An update may not list the issue it updates. Duplicate entries collapse.
- **The card.** One `context` block, only when `related` isn't empty. JotFlow builds each link as `https://github.com/{repo}/issues/{number}` from the validated fields. Its text is `repo#N (state)`, escaped like every other card field. There's no title, because a title is someone else's text, and the card already shows the agent's own body. A public repo's warning is unchanged.
- **"Same as".** A button on the pending card of a create proposal, for `related[0]` only, as #36's "best candidate".
  - **Its value** carries the proposal id, the content hash, and the candidate's index.
  - **Its checks** are the approval gate's own, in approval.py's order: workspace, guest or external, this card and channel, not paused, approvers. A stale hash is refused the same way.
  - **On success:**
    - The store links the topic to the issue with `set_topic_issue`, when the topic has no issue yet. If it already has one, the click is refused with a short reply.
    - The proposal becomes `rejected`, with `ProposalRecord.duplicate` set.
    - The card is redrawn in place to say "Same as" the issue, with its link. There's no separate reply or reaction.
  - **No GitHub write happens,** so no execution run, no token, and nothing for the runners.
- **The store.** One migration, 14, after #194's 13, adds six columns to proposals: `duplicate_repo`, `duplicate_number`, `duplicate_by`, `duplicate_at`, `duplicate_undone_by` and `duplicate_undone_at`, read back as `ProposalRecord.duplicate`. They're pointers and a Slack id only, for the card to show after closing, and for a wrong click to have an owner. The draft is cleared as for any final proposal.
- **The drafting instructions.** Section 5 keeps "ask instead of proposing" for an issue that looks like the same idea. It adds: list the issues you found that are close but not the same in `related`, most similar first, at most 3.

## Risks and open questions

1. **Decided by the architect: reuse `rejected`.** The options weighed were these. This is the architect's call.
   - A new `duplicate` state is clearest in the state diagram. But the proposals table has `CHECK (state IN (...))`, and SQLite can't alter a CHECK, so it would need a table rebuild in migration 14, and every state switch would have to handle it.
   - Reusing `rejected`, with the duplicate columns set, needs only `ALTER TABLE ... ADD COLUMN`s. The card tells the two apart by `ProposalRecord.duplicate`.
   - I'd reuse `rejected`.
2. **Decided by the architect: no "same as" on update proposals.** An update already targets an existing issue. `related` is still shown.
3. **Security.** New agent-supplied data reaches the card. It's three validated pointers, rendered by JotFlow, with no free text, and every repo is in the channel's allowlist. The click is a new state-changing action, so it takes the approval gate's full check set. Security to confirm.
4. **Auto mode.** A proposal in `auto` is written at once, so "same as" never appears. The card still lists `related` after the write, for a person to catch a duplicate and close it on GitHub.
5. **Older runners.** A runner or agent that doesn't send `related` is unaffected, because the field is optional.

## Decisions, 2026-10-20

- **The lead: who may click.** "Same as" follows exactly Approve's rules. In open mode, anyone in the channel may click it. With approvers on, only listed approvers may, never on a proposal drawn from their own messages. Guests and external users are always refused. It also takes every other approval check, including a second approver where the policy requires one.
- **The architect: reuse `rejected`,** with migration 14's ADD COLUMNs: `duplicate_repo`, `duplicate_number`, `duplicate_by`, `duplicate_at`, `duplicate_undone_by` and `duplicate_undone_at`. Three conditions:
  - **The card.** cards.py checks `ProposalRecord.duplicate` before the plain rejected text, and shows "Same as owner/name#N", marked by whom, instead of "Rejected. Nothing was written."
  - **Card sync.** card_sync.py's `_NO_CARD_NEEDED = {SUPERSEDED, REJECTED}` is for proposals nobody saw. A same-as proposal always had a visible card, so sync must still edit it into its final state, with a test.
  - **The invariant.** The duplicate columns are set together or not at all, and only with state `rejected`. It's enforced in both stores' code, since SQLite can't add a multi-column CHECK with ADD COLUMN, and a test covers it. architecture.md's state table says `rejected` with `duplicate` set means "same as an existing issue", not "people said no".
- **What follows a same-as click.** The topic is now linked to the issue, so a later message in the thread becomes a `propose_update` on that issue, which may be a closed one. The card's `(closed)` marker makes that visible before anyone clicks. usage.md says so.

## Security's review, 2026-10-20

- **The same rule as Approve, not stricter.** In open mode, Approve causes a GitHub write and "same as" causes none, so requiring an approver for the milder action would be inconsistent. With approvers on, it takes approvers. Authorship doesn't apply, because it settles nothing the person wrote. The real harm is that the binding persists: later approved updates comment on that issue. Hence the four controls below.
- **1. Trust only the stored draft.** The button's value carries the proposal id, the content hash and the index, and nothing else. The handler reads the repo and number from the stored draft's `related[index]`, never from the value. It checks that the proposal's topic is in the card's channel.
- **2. Record who clicked:** `duplicate_by`, the Slack id, stored with the issue in `ProposalRecord.duplicate`. The log line names ids only.
- **3. Undo, "Not a duplicate".** It's a button on the closed same-as card, under the same who-may rules.
  - It refuses if the topic now points at another issue, or if a proposal on the topic drafted after the click is pending, approved, executing, reconciling or applied, since it relies on the binding.
  - Otherwise it clears the topic's issue link, inside `Store.undo_duplicate`, which changes nothing if the topic has moved on. It records the undo on the proposal: the duplicate stays for the record, with `duplicate_undone_by` and `duplicate_undone_at`. It then queues a drafting run for the thread, so a fresh proposal appears.
  - The card shows "Same as acme-co/web#42, undone by @x".
- **4. The state is the agent's claim:** "(agent says closed)". JotFlow has no GitHub access to check it.
- **5. A threat-model row:** "Suppress a real issue by steering a wrong 'same as'". The defences are:
  - a person clicks, under Approve's rules
  - the target comes from the stored draft and is in the channel's repos
  - the click is recorded with who made it
  - it can be undone
  - no GitHub write happens
- **Confirmed as designed:** dropping out-of-allowlist repos with a warning, links built from RepoName and number only, escaping, and the content hash covering `related`.

## Progress checklist

- [x] Plan reviewed by the lead and the architect
- [x] Security answers question 3
- [x] Model, validation, and tool schema
- [x] Card rendering
- [x] "Same as" action, store transition, migration 14's six duplicate columns
- [x] "Not a duplicate" undo, the topic unbind inside `undo_duplicate`, and the redraft
- [x] Threat model row
- [x] Drafting instructions
- [x] Docs: usage.md, architecture.md's state diagram, threat model row if security asks
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- `make check` passes.
- Tests:
  - `related` outside the channel's repos is dropped
  - more than 3 entries are refused by the schema
  - the card's line and links
  - "same as" gate refusals, as for Approve
  - a stale hash is refused
  - a topic that already has an issue is refused
  - the reply and reaction on success
  - migration 14, upgrading 13 with existing rows
- A replay fixture where the stand-in agent fills `related`: bug-report.json, related to acme-co/web#17, closed.

## Handoff notes

- **Built,** with two simplifications:
  - The thread hears about a "same as" from the card itself. The card is edited in place in the thread, to "Same as acme-co/web#42, marked by @x", with the link, so there's no separate reply or reaction.
  - The undo is the card's **Not a duplicate** button, with no host CLI. Security asked for the button, or at least an operator path.
- **Who may click** is Approve's own check, factored out as `_check_may_approve` and shared by Approve, Same as and Not a duplicate. That includes `require_second_approver`, as the lead and the architect said. Security had said authorship needn't apply, so this is the stricter reading.
- **The undo's "since" test.** A proposal on the topic created after the "same as" that's still pending, approved, executing, reconciling or applied refuses the undo, as CodeRabbit asked. The memory store keeps no updated_at, so both stores use created_at.
- **The hash.** `related` is in `LATER_FIELDS`, so a draft without it hashes as before, and approvals in flight survive the upgrade.
- **`RelatedIssue` forbids extra fields,** so no title or note can ride along.
