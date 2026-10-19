# Channel modes: `/jotflow mode`

## Links and status

- Issue: #28
- Also serves: D2 (the mode is runtime state), D7 (who may change it, and the auto warning), #51's update to #28, #152 (the card), #27 (approval)
- Draft PR: #155
- Status: implementing
- Last verified: 2026-10-19, `make check` passes with the code below
- Next safe action: CodeRabbit, then ready for review

## Objective and done-when

Each channel runs in one of three modes, kept in the store and changed with a slash command:

- `propose`, the default: a card with Approve, and nothing written until a person approves
- `auto`: no approval step; the write happens at once, and the card is posted after it
- `paused`: JotFlow reads nothing and writes nothing

The mode applies to the whole lifecycle: creation, edits, comments, assignment, and labels.

Done when:

- [x] `/jotflow mode` shows the channel's mode privately, with who set it and when, and `/jotflow mode propose|auto|paused` sets it
- [x] With approvers off, any workspace member may change the mode. With approvers on, only an approver may. Guests and external users never may
- [x] Every change is announced in the channel, in plain text with no pings
- [x] Setting `auto` in a channel with guests or external members adds a warning to that announcement, as architecture.md's "Open mode, stated plainly" says
- [x] `paused` reads nothing and writes nothing:
  - no keyword handling, no debouncer arming, and no help notices for messages there
  - no run starts in that channel; queued runs wait until it is unpaused
  - no card is posted or edited there; they catch up after
  - Approve and the other card buttons are refused with a short private reply
- [x] `auto` approves each new proposal as "auto" and queues its execution run at once. The card is posted only when the write has finished, applied or failed, and it records "approved by auto mode"
- [x] `make check` passes, including the coverage floor

**Out of scope:** a default mode per channel in `jotflow.toml`. D2 keeps the mode as runtime state, and `propose` is the default.

## Design

- **Paused, in the store.** The rules go where every caller passes:
  - `claim_next_run` never claims a run in a channel whose stored mode is `paused`. That covers drafting, questions, execution, and the execute-fixture command. Both stores get it, with a contract test.
  - `cards_due` leaves out paused channels.
  - A run that is already running when the channel is paused is left to finish. Its writes are store writes, and the execution it might lead to cannot start.
- **Paused, at ingest.** `Orchestrator.note` returns before the keyword handler, the help handler, and the debouncer when the channel is paused. Reactions are dropped the same way. If the mode cannot be read, it fails closed and reads nothing.
- **Paused, on the card.** `ApprovalHandler` refuses any card action in a paused channel with "JotFlow is paused in this channel. Nothing is written until someone sets it back." This is #27's module. The change is one guard and its test, agreed with security.
- **Auto.** A new `AutoApprover` runs in the card loop, before `CardSync.sync`.
  - For each due proposal that is pending in an `auto` channel, it transitions it to approved, with `approved_by="auto"` and the stored content hash, and queues its execution run.
  - Proposals that were already pending when the channel switched to `auto`, and whose card is posted, are left for a person. Switching the mode never approves something someone was looking at.
  - `CardSync` does not post a card for a proposal approved by "auto" while it is approved, executing, or reconciling. It keys on `approved_by`, not the current mode, so it holds even if the mode changes mid-write. The card stays due and is posted once the proposal is applied or failed, so the summary card follows the write.
  - `cards.py` adds "Approved by auto mode" to an auto-approved card's later states, so the card posted after the write still says who approved it.
- **`orchestrator/modes.py`**:
  - `ModeCommand(store, policy, people, slack)` parses `/jotflow mode [propose|auto|paused]`.
  - It refuses in an unconfigured channel, and refuses guests, external users, and non-approvers when approvers are on.
  - It calls `set_channel_mode(changed_by=...)` and posts the announcement.
  - Setting the mode a channel already has is a private "already" reply, with no announcement.
- **The announcement**, as plain text: "<display name> set JotFlow to auto in this channel. <what that means>." In `auto`, it adds the warning when the channel is shared with another organisation, from `conversations.info`, or when a member is a guest, from `conversations.members` and the cached `users.info`. It checks the first 1000 members. A larger channel gets the warning anyway, worded as "could not check every member", so it fails closed.
- **`register_commands`** routes `mode` to `ModeCommand`. `help` stays as it is, and its mode line reads the stored mode as today.

## Risks and open questions

- **#27's file.** The paused guard in `approval.py` is security's module. I'll keep it to one guard and tell security.
- **The member scan** costs one `users.info` per member the first time. It runs only when someone sets `auto`, and it is cached.
- **A paused channel's queued runs** stay queued, so a long pause leaves stale drafting runs. They start when the channel is unpaused and read the thread as it is then, which is what the team wants.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Store: paused channels in `claim_next_run` and `cards_due`, both stores, contract tests
- [x] Orchestrator: paused at ingest, the mode lookup for cards, and `AutoApprover` in the card loop
- [x] `modes.py`: the command, permissions, announcement, auto warning
- [x] Approval: refuse in a paused channel
- [x] Tests through the real orchestrator and a real Bolt app, for each mode
- [x] Docs: architecture's approval gate, README's "Using it", `/jotflow help`
- [ ] CodeRabbit once, or skipped
- [x] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- `make check` passes.
- `tests/test_modes.py` runs on both stores. It covers:
  - showing, setting, the same mode again, a bad argument, and an unconfigured channel
  - guests, external users, and approvers
  - the plain-text announcement with no pings, and the auto warning only for auto
  - `AutoApprover` approving new proposals and queuing their writes, leaving propose channels and already-shown cards alone, and skipping a lost transition
  - routing `/jotflow mode`
  - the Bolt adapter's shared-channel and guest scan across pages, the 1000-member cap, and a plain announcement
- `tests/test_store_runs.py` shows a paused channel starts no run and draws no card until it is unpaused, on both stores.
- `tests/test_approval.py` shows every card action refused in a paused channel, then working again.
- `tests/test_card_sync.py` shows an auto-approved card posted only after the write, saying "Approved by auto mode", and a person-approved one not held.
- `tests/test_service.py` runs the real orchestrator. It shows a paused channel reads nothing, an unreadable mode fails closed, and auto mode approves, launches the execution run on the exec runner, holds the card, and posts it after the write.

## Handoff notes

None yet.
