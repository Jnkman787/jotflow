# Post and update the proposal card in the live service

## Links and status

- Issue: #152
- Also serves: #26 and #120 (the card), #27 (its buttons need a posted card), #28 (auto mode), #122 (the public-repo warning), D7
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-19, no code yet
- Next safe action: add the store's card revision, after #150's migration 7 merges

## Objective and done-when

The card is built and tested, but nothing in `jotflow run` posts it. After this, every proposal gets one card in its thread, and that same message follows the proposal through every state.

Done when:

- [ ] A drafting run that proposes an issue yields one card in the thread, with `card_ts` stored, tested through the real orchestrator with a fake Slack
- [ ] Every state change edits the same card, and a superseded proposal's card says so
- [ ] In `auto` mode the card still posts, with the auto-approval state line
- [ ] A Slack failure leaves the proposal intact and is retried, with a test. Posting never runs inside an MCP tool's write path
- [ ] No agent text is ever posted as mrkdwn
- [ ] `make check` passes, including the coverage floor

**Out of scope:**
- Looking up a repo's visibility on GitHub. JotFlow holds no GitHub token, so the card keeps saying "visibility was not checked" until a later issue supplies it.
- The buttons' handlers, which are security's #27.

## Design

- **The store is the outbox.** Migration 8 adds two columns to `proposals`:
  - `revision`, an integer that every change a card shows increases: `add_proposal`, `transition`, `clear_draft`, `supersede_pending`, and a result being recorded
  - `card_revision`, the revision the posted card shows, or null before the first post
  - `set_card_ts` changes neither, since it changes nothing on the card.
  - `ProposalRecord` gains both, read-only, outside the content hash.
  - `Store.cards_due(limit)` returns the proposals whose card is behind: `card_revision` null or below `revision`, oldest first.
  - `Store.mark_card_shown(id, revision)` raises `card_revision` to that revision, never lowers it. So a transition that lands while a card is being edited leaves the card behind, and it is edited again on the next pass.
  - Both stores implement them, and a contract test covers both.
  - This is the "one subscriber on the store's transitions" the issue asks for. Nothing scattered calls Slack after a write. The store records that a card is stale, and one loop catches up. That holds across a restart too.
- **`orchestrator/card_sync.py`**, which is mine. Security owns `cards.py`'s rendering for #27.
  - `card_context(policy, store, people, record)` builds the full `CardContext`: the channel's policy, display names for the participants and the approver, and their identity links from the store.
  - `CardSync(store, policy, publisher, people)` has two methods:
    - `sync()` takes each due proposal and decides:
      - a proposal with no card yet, that is pending or later (approved, executing, reconciling, applied, failed), gets posted
      - one that was superseded or rejected before any card went up gets none, and is marked shown so it stops coming back
      - anything else gets its card edited in place
    - `refresh(proposal_id)` does the same for one proposal right away. #27's handlers can call it after a transition, though the loop would catch up within a second anyway.
  - The orchestrator runs `sync()` in its own loop, about once a second, next to the debouncer's flush loop, and never in the MCP write path. A Slack error is logged, and the proposal stays due, so the next pass retries. Before giving up on a proposal there is no limit, but the loop paces itself: a failed proposal waits longer before its next try, up to a minute, so a Slack outage does not flood the log.
- **`GiveUpNotice`** (#30) keeps its thread reply for a proposal with no card. For one with a card, it only needs the transition, and `CardSync` re-renders it. That removes the second place that re-renders a card.
- **Auto mode.** The proposal is approved at once, and the card is posted already approved, with the auto-approval state line that `cards.py` renders today.

## Risks and open questions

- **Migration numbers.** #150 adds migration 7, so this is 8. Whichever lands later renumbers.
- **#27 overlap.** Agreed with security: they own `cards.py` rendering, `Debouncer.arm`, and the shared guest and external check. I own `card_sync.py`, the store's card revision, and the orchestrator wiring for cards. Their handlers transition proposals and let `CardSync` edit the card, or call `refresh`.
- **Existing databases** get `revision = 1` and `card_revision = 1` where `card_ts` is set, and null otherwise. A database with old pending proposals gets their cards posted once after the upgrade. Pending proposals had no card before, so that is wanted.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Store: migration 8, `revision`, `card_revision`, `cards_due`, `mark_card_shown`, both stores, contract and schema tests
- [ ] `card_sync.py`: `card_context`, `CardSync.sync` and `refresh`, with backoff
- [ ] Orchestrator: the sync loop; `GiveUpNotice` relies on it
- [ ] Tests: through the real orchestrator, a proposal posts one card, transitions edit it, supersede, auto mode, Slack failure and retry, plain text only
- [ ] Docs: architecture's card section, and the plan's handoff for #27
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
