# Reaction approval, and tool-level policy tests

## Links and status

- Issue: #34
- Also serves: #29, the tool-level tests
- Draft PR: pending
- Status: implementing
- Last verified: plan only
- Next safe action: implement, then mark ready

## Objective and done-when

#151 built the Approve button's gate. #34 adds the optional reaction that counts as Approve, and #29 proves at the tool level that a proposal outside the policy never becomes a card.

**#29:**

- [ ] `propose_issue` and `propose_update` each have three cases: a repo the channel doesn't feed, a label outside a repo's set list, and an unknown channel. Each returns `isError` with the policy's reason, through the real MCP server.
- [ ] In those cases no proposal is stored, so no card is due, and nothing reaches Slack: the card sync loop draws nothing.

**#34:**

- [x] A non-approver who presses Approve gets an ephemeral refusal (#151).
- [ ] Reaction approval is off by default and configurable. `[approval] reactions = false` stays the switch, and a new `reaction = "white_check_mark"` names the emoji. The name is validated as a Slack emoji name, and it can't be a keyword reaction such as `no_entry_sign`.
- [ ] With reactions on, the configured reaction on a pending proposal's card counts as Approve. It runs every check #151 runs:
  - this workspace's team for the event, and the person's team through `users.info`
  - no guests or external users
  - the proposal's own card in its own channel
  - the approver list, and authorship from Slack
  - the hash lock
- [ ] With reactions off, or with another emoji, the reaction is ignored silently, and nothing changes.
- [ ] It's tested through the real Bolt app, with `reaction_added` events.

## Design

**The hash of the card as shown.** A reaction carries no hash, unlike the button. The card shows the stored draft only once `card_revision == revision`, which is `not record.card_due` (#152). So a reaction approves only when the card is up to date and the proposal is pending, locking `content_hash(record.proposal)`. A card that's behind gets an ephemeral reply, "This card is being updated; react again in a moment", and nothing changes.

**Too long to approve.** A card whose draft was too long to show in full has no Approve button, so a reaction mustn't approve it either. `cards.approvable(record, context)` is factored out of `render_card` and uses the same layout rule. The reaction path checks it with `card_context`, the context the card was drawn with.

**Finding the proposal.** A new `Store.proposal_for_card(channel_id, card_ts)` returns the proposal whose card is that message, in both stores. It's a lookup by existing columns, so there's no migration.

**Wiring.** `ApprovalHandler.on_reaction(event, team_id)` builds the same checked click as a button press: user, channel, the card's ts, and the content hash as the value. It then runs `_guarded(..., _approve)`. The orchestrator's `note()` passes accepted reactions to it after the keyword handler. The envelope's `team_id` stands in for `body.team.id`. A reaction has no `user.team_id`, so `users.info` decides `external`, as it already does.

## Risks and open questions

- **The policy key** is to be agreed with the architect. My default is above, and it keeps existing files valid.
- **Duplicate deliveries.** Slack may redeliver an event, but ingest drops event ids it has seen (#8). A second reaction gets "already approved".
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [ ] #29 tool-level tests
- [ ] Policy key, writer, and example file
- [ ] `proposal_for_card` in both stores, with contract tests
- [ ] `cards.approvable`, factored out with no rendering change
- [ ] `on_reaction`, and its orchestrator wiring
- [ ] Real-app tests
- [ ] Docs: architecture's approval section and the README's policy table
- [ ] `make check` passes

## Validation

Pending.

## Handoff notes

Worktree `~/jotflow-work/wt-security-m4`, branch `feat/policy-tests-reaction-approval`.
