# The card's repo dropdown and assign buttons

## Links and status

- Issue: #160
- Also serves: #26 and #120 (the card renders the controls), #27 and #151 (who may click), #122 (repo visibility), #152 (the card redraws), D7, D11
- Draft PR: #163, rebased onto main after #164
- Status: ready for review
- Last verified: 2026-10-19, `make check` passes with the code below
- Next safe action: review

## Objective and done-when

The card shows a repo dropdown and Make assignee and Remove buttons, but nothing handles them. A click is never acknowledged, so Slack shows a warning. After this, each control edits the pending proposal, the card redraws, and an Approve on the old card is stale.

Done when:

- [x] Each control works through the real Bolt app. There are tests for every refusal: a guest, another workspace, the wrong card, a paused channel, a repo outside the channel, and an assignee with no linked GitHub login
- [x] An edit changes the content hash and redraws the card, and an Approve from before the edit is refused as stale
- [x] An edit on an approved, executing, reconciling, applied, rejected, or superseded proposal is refused
- [x] Every click is acknowledged within Slack's three seconds
- [x] The edit is recorded with who, when, and which field, never text
- [x] `make check` passes, including the coverage floor

**Out of scope:** editing the title, body, or labels by hand. Request Changes covers that.

## Design

- **Who may click** (D7, #151). Through `approval.py`'s own checks: a member of this workspace, not a guest or external user, on the proposal's own card in its own channel, and refused in a paused channel. With approvers on, only an approver may edit. A source author may, since editing is not approving: the approver-list check applies, and the not-your-own-proposal check does not.
- **`approval.py`**, security's module. It exposes one public entry point, `ApprovalHandler.guarded(body, action)`, which is today's `_guarded`, and exports `Refused`. That is being agreed with security.
- **`orchestrator/card_edits.py`**, mine. `CardEdits(store, policy, guarded, refresh)`:
  - **The repo** comes from the dropdown's `selected_option.value`, `<proposal id>:<nickname>` (security's #164; a nickname fits Slack's 150-character limit where owner/name might not). It must match a nickname in the channel's policy exactly. An owner/name, or a nickname the channel doesn't list, is refused with a fixed private reply (review). Labels the new repo does not allow are dropped, and the card says so. A repo change on an update proposal is refused, since its issue number belongs to its repo. The topic stays.
  - **Make assignee** takes the button's Slack user id. It must be a participant with a linked GitHub login, the same rule as `propose_issue`. The login is added once.
  - **Remove** takes the login. It is removed from the assignees only, never from the participants.
  - **On every edit**, the draft is re-validated through the model, the store saves it, and `refresh(proposal_id)` redraws the card. A private, fixed reply confirms it, and nothing from the draft is quoted.
- **The store**, `edit_proposal(proposal_id, draft, *, by, field)`:
  - It refuses anything but pending or failed. A failed proposal keeps its old approval fields, so a Retry approves the new hash.
  - It saves the draft, raises the revision so `CardSync` redraws the card, and records a row in a new `proposal_edits` table: the proposal, who, when, and the field, as `repo`, `assign`, or `unassign`, never the value.
  - That is migration 10. Both stores, with contract tests. A repo change also drops the agent's `repo_visibility` (#159), which was about the old repo.
- **Staleness.** Approve carries the content hash of the card it was clicked on. An edit changes the hash, so an old card's Approve gets the existing "out of date" refusal, and the card redraws.
- **Registration.** `register_edits(app, edits)` beside `register_actions`. Each handler acks before doing anything else.

## Risks and open questions

- **The `approval.py` entry point** (asked of security). If they would rather keep `_guarded` private, the alternative is a small shared guard module both handlers use.
- **#122** adds repo visibility to the proposal. Whichever lands second wires "a repo change clears it".
- **Migration numbers.** 10 is free today.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; the entry point pending security
- [x] Commit this plan and open the draft PR
- [x] Store: migration 10, `edit_proposal`, `proposal_edits`, both stores, contract tests
- [x] `approval.py`: the public `guarded` entry point (security's call)
- [x] `card_edits.py`: repo, assign, unassign, with refusals
- [x] Registration and ack-first, wired in `jotflow run`
- [x] Tests through the real Bolt app, for every refusal, staleness, and the redraw
- [x] Docs: architecture's card section
- [x] CodeRabbit once: skipped, the free CLI allowance was rate-limited
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` passes.
- `tests/test_card_edits.py` dispatches real Slack payloads through a real Bolt app with `register_actions` and `register_edits`. Every click returns 200, acknowledged. It covers:
  - moving to another repo, with its labels and visibility report dropped, the card redrawn, a private note, and an audit row
  - choosing the same repo, a repo outside the channel, and an update keeping its repo
  - assigning a linked participant once, and refusing an unlinked participant and a stranger
  - removing an assignee while the participant stays
  - guests, external users, another workspace, another card, and a paused channel
  - approvers, where a source author may edit, and approved or rejected proposals
  - an Approve from before an edit refused as stale, then the new card approving
  - a failing edit still acknowledged and logged
- The reviewer's six checks are tests by name, all through the real app: members edit with approvers off; a non-approver is refused with approvers on; an author approver edits but cannot approve; the old card's hash is stale and another approver approves the edit; an unknown nickname is refused; a repo change clears `repo_visibility`.
- `tests/test_store_contract.py` covers `edit_proposal` on both stores: the redraw, staleness, editing and retrying a failed proposal, the refused states, kind and topic kept, and a repo change dropping the visibility report.
- `tests/test_run.py` shows `jotflow run` registers the three controls.

## Handoff notes

None yet.
