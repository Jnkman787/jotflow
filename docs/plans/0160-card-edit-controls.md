# The card's repo dropdown and assign buttons

## Links and status

- Issue: #160
- Also serves: #26 and #120 (the card renders the controls), #27 and #151 (who may click), #122 (repo visibility), #152 (the card redraws), D7, D11
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-19, no code yet
- Next safe action: agree the `approval.py` entry point with security, then the store's `edit_proposal`

## Objective and done-when

The card shows a repo dropdown and Make assignee and Remove buttons, but nothing handles them. A click is never acknowledged, so Slack shows a warning. After this, each control edits the pending proposal, the card redraws, and an Approve on the old card is stale.

Done when:

- [ ] Each control works through the real Bolt app. There are tests for every refusal: a guest, another workspace, the wrong card, a paused channel, a repo outside the channel, and an assignee with no linked GitHub login
- [ ] An edit changes the content hash and redraws the card, and an Approve from before the edit is refused as stale
- [ ] An edit on an approved, executing, reconciling, applied, rejected, or superseded proposal is refused
- [ ] Every click is acknowledged within Slack's three seconds
- [ ] The edit is recorded with who, when, and which field, never text
- [ ] `make check` passes, including the coverage floor

**Out of scope:** editing the title, body, or labels by hand. Request Changes covers that.

## Design

- **Who may click** (D7, #151). Through `approval.py`'s own checks: a member of this workspace, not a guest or external user, on the proposal's own card in its own channel, and refused in a paused channel. With approvers on, only an approver may edit. A source author may, since editing is not approving: the approver-list check applies, and the not-your-own-proposal check does not.
- **`approval.py`**, security's module. It exposes one public entry point, `ApprovalHandler.guarded(body, action)`, which is today's `_guarded`, and exports `Refused`. That is being agreed with security.
- **`orchestrator/card_edits.py`**, mine. `CardEdits(store, policy, guarded, refresh)`:
  - **The repo** comes from the dropdown's `selected_option.value`. It must be one of the channel's repos. Labels the new repo does not allow are dropped, and the card says so. A repo change on an update proposal is refused, since its issue number belongs to its repo. The topic stays.
  - **Make assignee** takes the button's Slack user id. It must be a participant with a linked GitHub login, the same rule as `propose_issue`. The login is added once.
  - **Remove** takes the login. It is removed from the assignees only, never from the participants.
  - **On every edit**, the draft is re-validated through the model, the store saves it, and `refresh(proposal_id)` redraws the card. A private, fixed reply confirms it, and nothing from the draft is quoted.
- **The store**, `edit_proposal(proposal_id, draft, *, by, field)`:
  - It refuses anything but pending or failed. A failed proposal keeps its old approval fields, so a Retry approves the new hash.
  - It saves the draft, raises the revision so `CardSync` redraws the card, and records a row in a new `proposal_edits` table: the proposal, who, when, and the field, as `repo`, `assign`, or `unassign`, never the value.
  - That is migration 10. Both stores, with contract tests. When #122 lands, a repo change also clears its stored visibility.
- **Staleness.** Approve carries the content hash of the card it was clicked on. An edit changes the hash, so an old card's Approve gets the existing "out of date" refusal, and the card redraws.
- **Registration.** `register_edits(app, edits)` beside `register_actions`. Each handler acks before doing anything else.

## Risks and open questions

- **The `approval.py` entry point** (asked of security). If they would rather keep `_guarded` private, the alternative is a small shared guard module both handlers use.
- **#122** adds repo visibility to the proposal. Whichever lands second wires "a repo change clears it".
- **Migration numbers.** 10 is free today.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; the entry point pending security
- [ ] Commit this plan and open the draft PR
- [ ] Store: migration 10, `edit_proposal`, `proposal_edits`, both stores, contract tests
- [ ] `approval.py`: the public `guarded` entry point (security's call)
- [ ] `card_edits.py`: repo, assign, unassign, with refusals
- [ ] Registration and ack-first, wired in `jotflow run`
- [ ] Tests through the real Bolt app, for every refusal, staleness, and the redraw
- [ ] Docs: architecture's card section
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
