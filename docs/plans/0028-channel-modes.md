# Channel modes: `/jotflow mode`

## Links and status

- Issue: #28
- Also serves: D2 (the mode is runtime state), D7 (who may change it, and the auto warning), #51's update to #28, #152 (the card), #27 (approval)
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-19, no code yet
- Next safe action: the store rules for paused channels

## Objective and done-when

Each channel runs in one of three modes, kept in the store and changed with a slash command:

- `propose`, the default: a card with Approve, and nothing written until a person approves
- `auto`: no approval step; the write happens at once, and the card is posted after it
- `paused`: JotFlow reads nothing and writes nothing

The mode applies to the whole lifecycle: creation, edits, comments, assignment, and labels.

Done when:

- [ ] `/jotflow mode` shows the channel's mode privately, with who set it and when, and `/jotflow mode propose|auto|paused` sets it
- [ ] With approvers off, any workspace member may change the mode. With approvers on, only an approver may. Guests and external users never may
- [ ] Every change is announced in the channel, in plain text with no pings
- [ ] Setting `auto` in a channel with guests or external members adds a warning to that announcement, as architecture.md's "Open mode, stated plainly" says
- [ ] `paused` reads nothing and writes nothing:
  - no keyword handling, no debouncer arming, and no help notices for messages there
  - no run starts in that channel; queued runs wait until it is unpaused
  - no card is posted or edited there; they catch up after
  - Approve and the other card buttons are refused with a short private reply
- [ ] `auto` approves each new proposal as "auto" and queues its execution run at once. The card is posted only when the write has finished, applied or failed, and it records "approved by auto mode"
- [ ] `make check` passes, including the coverage floor

**Out of scope:** a default mode per channel in `jotflow.toml`. D2 keeps the mode as runtime state, and `propose` is the default.

## Design

- **Paused, in the store.** The rules go where every caller passes:
  - `claim_next_run` never claims a run in a channel whose stored mode is `paused`. That covers drafting, questions, execution, and the execute-fixture command. Both stores get it, with a contract test.
  - `cards_due` leaves out paused channels.
  - A run that is already running when the channel is paused is left to finish. Its writes are store writes, and the execution it might lead to cannot start.
- **Paused, at ingest.** `Orchestrator.note` returns before the keyword handler, the help handler, and the debouncer when the channel is paused. Reactions are dropped the same way.
- **Paused, on the card.** `ApprovalHandler` refuses any card action in a paused channel with "JotFlow is paused in this channel. Nothing is written until someone sets it back." This is #27's module. The change is one guard and its test, agreed with security.
- **Auto.** A new `AutoApprover` runs in the card loop, before `CardSync.sync`.
  - For each due proposal that is pending in an `auto` channel, it transitions it to approved, with `approved_by="auto"` and the stored content hash, and queues its execution run.
  - Proposals that were already pending when the channel switched to `auto`, and whose card is posted, are left for a person. Switching the mode never approves something someone was looking at.
  - `CardSync` gets a mode lookup. In an `auto` channel it does not post a card while the proposal is approved, executing, or reconciling. The card stays due and is posted once the proposal is applied or failed, so the summary card follows the write.
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
- [ ] Commit this plan and open the draft PR
- [ ] Store: paused channels in `claim_next_run` and `cards_due`, both stores, contract tests
- [ ] Orchestrator: paused at ingest, the mode lookup for cards, and `AutoApprover` in the card loop
- [ ] `modes.py`: the command, permissions, announcement, auto warning
- [ ] Approval: refuse in a paused channel
- [ ] Tests through the real orchestrator and a real Bolt app, for each mode
- [ ] Docs: architecture's approval gate, README's "Using it", `/jotflow help`
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
