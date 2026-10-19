# Approve and Request Changes

## Links and status

- Issue: #27
- Also serves: #30, the attempt reset on a new approval
- Draft PR: #151
- Status: ready for review
- Last verified: `make check` green, 1829 tests, 98.9% coverage, on 2026-10-19
- Next safe action: reviewer reviews #151. Product wires the first card post

## Objective and done-when

This is the approval gate (D7). A person's click on a card's Approve button becomes an approved proposal and a queued execution run, but only if the person may approve and the card shows exactly what is stored. Request Changes turns a person's text into a thread reply that the next drafting run reads (D9).

The issue's boxes:

- [x] Only permitted approvers can approve. With approvers off, the default, anyone in the channel may approve. With approvers on, only listed approvers may, and never one of the proposal's participants. Guests and external Slack Connect users can never approve.
- [x] The Request Changes text appears in the thread attributed to the person.
- [x] The card shows who approved and when. The existing approved block shows it once the card is re-rendered after approval.

From the lead's brief and the dated notes:

- [x] Approve sends the content hash of the draft the card showed (#26). The transition locks it (`expected_hash`), and a stale card gets a clear ephemeral reply, with nothing approved.
- [x] The approver and the time are recorded, and one execution run is queued. The store already coalesces it per proposal.
- [x] Interaction payloads are checked:
  - JotFlow runs in Socket Mode, so Slack's signature is replaced by the app-token websocket, which Bolt authenticates. There is no signing secret to check, and the plan says so plainly.
  - The handler checks that the payload's team and the user's team are JotFlow's own.
  - The channel must be the card's channel, and the clicked message must be the stored card (`card_ts`).
  - The button value must be exactly `<proposal id>:<sha256>`.
- [x] Each new approval resets the #30 attempt count. Attempts count only execution runs created at or after `approved_at`. A reconciling retry keeps its approval and so keeps counting.
- [x] A failed card offers **Retry** while the draft exists, which is an approval with the card's hash. It also offers **Discard**, which calls `clear_draft` (from #102 and #103).

**Out of scope:**

- Posting the first card after `propose_issue`. I'll ask product who wires that, since #120 built the publisher.
- Reaction approval (`approval.reactions`, off by default).
- `auto` mode and `/jotflow mode` (#28 and the channel-mode issue).

## Design

**`orchestrator/approval.py`** adds `ApprovalHandler(store, policy, slack, cards, own_team, arm_thread)`. It reads `slack` (`ApprovalSlack`), a small protocol with three calls: `person(user_id)`, `ephemeral(channel, user, text)`, and `open_modal(trigger_id, view)`. Bolt's adapter implements it. `person` delegates to `BoltKeywordSlack.person` (#135), so both use the same guest and external test without moving product's code.

**Approve, in order. Every refusal is a fixed ephemeral reply and writes nothing.**

1. Parse and check the payload. `user.id`, `team.id`, `channel.id`, `container.message_ts`, and the action value must all be present and well formed. `team.id` and `user.team_id` must both be JotFlow's own team, from `auth.test`.
2. Look up the person. A guest or an external user is refused.
3. Look up the proposal. It must exist, its `channel_id` must be the payload's channel, and its `card_ts` must be the clicked message.
4. Check the approver policy. With approvers on, the user must be listed and must not be one of the proposal's participants.
5. Transition to approved, with `expected={PENDING, FAILED}`, `approved_by=user` and `expected_hash`:
   - `ContentChangedError` gets "This card is out of date. Nothing was approved; the card has been refreshed." and the card is re-rendered.
   - `TransitionError` gets "This proposal is already <state>."
   - `DraftClearedError` gets "This draft was discarded."
6. Enqueue the execution run, then re-render the card. Its approved block shows who and when.

**Request Changes:**

- On the button: the same checks as Approve for steps 1 to 3, for any pending proposal. Then it opens a modal, with `private_metadata` set to the proposal id and one multiline `plain_text_input` of at most 2000 characters.
- On submit: the same team and person checks again. A refusal goes back into the dialog as a field error, since a dialog has no channel for an ephemeral. The proposal must still be pending in that channel. JotFlow then posts one reply in the proposal's thread: `Change request from <@U…>:` followed by the text, cleaned like `ask_channel` with broadcast mentions removed and Slack markup escaped, never unfurled. Then it arms the thread with the debouncer.
- The arming is needed because ingest drops JotFlow's own messages, so the reply alone would never start a drafting run. The new `Debouncer.arm(thread)` does what `note` does for an accepted message.
- The packet already keeps JotFlow's messages, with `from_jotflow`. I'll update that field's description to cover change requests, which name the person in their text.

**Attempt reset:** `RunLifecycle._attempts` counts only runs with `created_at >= record.approved_at`, when `approved_at` is set.

**Bolt wiring:** `register_actions(app, handler)` registers `app.action(ACTION_APPROVE)`, `ACTION_REQUEST_CHANGES`, `ACTION_RETRY`, `ACTION_DISCARD`, and `app.view(CALLBACK_CHANGES)`. Each acks immediately. The orchestrator builds the handler once runs are on, from Bolt's client and the identity.

**Files shared with product:** `cards.py` gains the failed card's Retry and Discard buttons and their action ids, next to #149's hint. `debounce.py` gains `arm`. `keyword_handler.py` is unchanged, since the adapter delegates to it.

**Review changes, from the lead and product:**

- **Self-approval** means writing a source message, not being a participant. `participants` is agent-written: the poster plus anyone mentioned, and an agent could leave the poster out. So with approvers on, the gate asks Slack, through `conversations.replies`, who wrote the thread's first message and each cited source message in the channel. The approver must have written none of them. It fails closed: a message Slack can't find, or more than 25 to check, refuses the approval. Nothing new is stored, so no migration is needed.
- **The guest and external check** moved to `orchestrator/people.py` as `SlackPeople`, with no behaviour change: it still fails closed, and it still calls auth.test once. `BoltKeywordSlack` and `BoltApprovalSlack` both delegate to it, and `keyword_handler` re-exports `Person`.
- **`Debouncer.arm`** doesn't override a person's skip. If the thread's first message is person chatter, `queue_pending` drops the thread at flush (#145), so a change request can't redraft a skipped thread. That's intended: the packet refuses such a thread anyway.
- **The live card is #152, product's, now merged.** The handlers call `orchestrator.cards.refresh(proposal_id)` and never draw a card themselves, so the card's revision is recorded.
- **Card names stay on #152's `BoltPeople`, not `SlackPeople`.** Display names may be cached for the life of the process. The guest and external check must not be: a person's status can change, and approval rests on it. So the two lookups stay separate on purpose.
- **Bot-authored messages.** A message Slack returns with a `bot_id`, or the `bot_message` subtype, and no user has no person to exclude. It adds no author and doesn't refuse, so a thread an alert bot started can still be approved. A message that can't be found at all, such as a deleted one, still refuses.
- **An accepted gap.** Only the thread's first message and the messages the proposal cites are checked, not uncited replies in the thread. An agent chooses what it cites, within its packet. That matches "source messages" in the rule, and the first message is the usual proposer, so the reviewer and the lead accepted it.

## Risks and open questions

- **Request Changes from a guest.** My default is to refuse guests and external users for every action, as the brief says. A guest can still just reply in the thread.
- **The card's first posting** is #152, which is product's. Until it lands, these handlers are exercised through tests only.
- **CardContext.** Re-rendering needs one, so I'll use the builder the first-post path uses. If there isn't one yet, I'll use a minimal channel-policy context, as #148 does.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [x] Confirm the issue is ready
- [x] Commit this plan and open the draft PR
- [x] `ApprovalHandler`: Approve, Retry, Discard, and Request Changes, with refusals, and tests for each check
- [x] The attempt reset in `RunLifecycle`, with a test
- [x] Card buttons for the failed state, coordinated with #149
- [x] `Debouncer.arm`, and the Bolt registration and orchestrator wiring
- [x] Docs: architecture's approval section, and how Socket Mode replaces the signing secret
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

`make check` is green, with 1829 tests. `approval.py` is at 99%.

- `tests/test_approval.py` covers:
  - an approval that locks the hash, records the approver and time, queues one run, and redraws the card;
  - a stale hash approving nothing, with a redrawn card;
  - another team, a user from another team, and a malformed user id;
  - guests and external users;
  - another channel, with no reply there, and another message;
  - malformed values and payloads;
  - approvers on: not listed, a participant, and a listed non-participant;
  - approvers off with the poster approving;
  - a second approval and a lost race;
  - Retry, and Discard followed by Retry refused, plus Discard's state and approver rules;
  - the Request Changes dialog's contents, and its pending-and-trigger rules;
  - a posted change request: attributed, broadcasts removed, markup escaped, thread armed;
  - empty, guest, external, gone, and settled change requests;
  - the Bolt wiring: ack first, dialog errors, and errors logged without the payload;
  - the Bolt adapter: no link or mention expansion, and auth.test called once.
- `tests/test_runs.py`: a new approval after a failure starts a fresh attempt count.
- `tests/test_cards.py`: a failed card's Retry carries the hash, Discard is red, both disappear once discarded, and a card too long to show offers only Discard.
- `tests/test_run.py`: serve registers the four actions and the dialog, and stops the orchestrator if registration fails.

## Handoff notes

Worktree `~/jotflow-work/wt-security-approve`, branch `feat/approval-actions`.
