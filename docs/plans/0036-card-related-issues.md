# Related issues on the card, and "same as"

## Links and status

- Issue: #36 (M6: After v0.1.0)
- Draft PR: to be opened
- Status: plan, with the lead's and the architect's calls recorded on 2026-10-20. Waiting on security before code
- Last verified: 2026-10-20, against main
- Next safe action: review the open questions below

## Objective and done-when

The drafting agent already searches for duplicates (drafting.md section 5). When it finds an issue that looks like the *same* idea, it asks in the thread instead of proposing. When it finds issues that are only *related*, it proposes, and an approver never sees what it found. A duplicate it misjudged as related then reaches GitHub.

Done when:

- [ ] `propose_issue` and `propose_update` accept an optional `related` list: up to 3 issue references the agent found, each `owner/name#N` plus `open` or `closed`
- [ ] The card shows them in one context line, such as `Related: acme-co/web#42 (closed) · acme-co/web#57 (open)`, each linked to its GitHub issue by JotFlow
- [ ] The card offers **Same as acme-co/web#42** for the first candidate on a pending create proposal. A person allowed to approve can click it. JotFlow then:
  - links the proposal's topic to that issue
  - closes the proposal without writing anything to GitHub
  - replies in the thread with the existing issue's link, as it does after a write
  - shows on the card who marked it, and as the same as what
- [ ] Nothing new is stored but pointers: repo, number and state. No issue title or body ever reaches the card or the store
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
    - The proposal moves to a closed state, with `duplicate_of` set. See the open questions.
    - JotFlow posts the thread reply "Already tracked in acme-co/web#42: <link>" through the existing link-reply path, and the source messages get the issue reaction.
  - **No GitHub write happens,** so no execution run, no token, and nothing for the runners.
- **The store.** One migration, 14, after #194's 13, adds `duplicate_of_repo` and `duplicate_of_number` to proposals, which are pointers only, for the card to show after closing. The draft is cleared as for any final proposal.
- **The drafting instructions.** Section 5 keeps "ask instead of proposing" for an issue that looks like the same idea. It adds: list the issues you found that are close but not the same in `related`, most similar first, at most 3.

## Risks and open questions

1. **Decided by the architect: reuse `rejected`.** The earlier options were: This is the architect's call.
   - A new `duplicate` state is clearest in the state diagram. But the proposals table has `CHECK (state IN (...))`, and SQLite can't alter a CHECK, so it would need a table rebuild in migration 14, and every state switch would have to handle it.
   - Reusing `rejected`, with `duplicate_of_repo` and `duplicate_of_number` set, needs only two `ALTER TABLE ... ADD COLUMN`s. The card tells the two apart by `duplicate_of`.
   - I'd reuse `rejected`.
2. **Decided by the architect: no "same as" on update proposals.** An update already targets an existing issue. `related` is still shown.
3. **Security.** New agent-supplied data reaches the card. It's three validated pointers, rendered by JotFlow, with no free text, and every repo is in the channel's allowlist. The click is a new state-changing action, so it takes the approval gate's full check set. Security to confirm.
4. **Auto mode.** A proposal in `auto` is written at once, so "same as" never appears. The card still lists `related` after the write, for a person to catch a duplicate and close it on GitHub.
5. **Older runners.** A runner or agent that doesn't send `related` is unaffected, because the field is optional.

## Decisions, 2026-10-20

- **The lead: who may click.** "Same as" follows exactly Approve's rules. In open mode, anyone in the channel may click it. With approvers on, only listed approvers may, never on a proposal drawn from their own messages. Guests and external users are always refused. It also takes every other approval check, including a second approver where the policy requires one.
- **The architect: reuse `rejected`,** with `duplicate_of_repo` and `duplicate_of_number` added by two ADD COLUMNs in migration 14. Three conditions:
  - **The card.** cards.py checks `duplicate_of` before the plain rejected text, and shows "Same as owner/name#N", marked by whom, instead of "Rejected. Nothing was written."
  - **Card sync.** card_sync.py's `_NO_CARD_NEEDED = {SUPERSEDED, REJECTED}` is for proposals nobody saw. A same-as proposal always had a visible card, so sync must still edit it into its final state, with a test.
  - **The invariant.** Either both duplicate_of columns are set or neither, and only with state `rejected`. It's enforced in both stores' code, since SQLite can't add a multi-column CHECK with ADD COLUMN, and a test covers it. architecture.md's state table says `rejected` with duplicate_of means "same as an existing issue", not "people said no".
- **What follows a same-as click.** The topic is now linked to the issue, so a later message in the thread becomes a `propose_update` on that issue, which may be a closed one. The card's `(closed)` marker makes that visible before anyone clicks. usage.md says so.

## Progress checklist

- [x] Plan reviewed by the lead and the architect
- [ ] Security answers question 3
- [ ] Model, validation, and tool schema
- [ ] Card rendering
- [ ] "Same as" action, store transition, migration 13
- [ ] Drafting instructions
- [ ] Docs: usage.md, architecture.md's state diagram, threat model row if security asks
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
- A replay fixture where the stand-in agent fills `related`.

## Handoff notes

None yet.
