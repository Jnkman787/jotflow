# Reaction approval, and tool-level policy tests

## Links and status

- Issue: #34
- Also serves: #29, the tool-level tests
- Draft PR: #154
- Status: ready for review
- Last verified: `make check` green, 2018 tests, 98.8% coverage, on 2026-10-19
- Next safe action: reviewer reviews #154

## Objective and done-when

#151 built the Approve button's gate. #34 adds the optional reaction that counts as Approve, and #29 proves at the tool level that a proposal outside the policy never becomes a card.

**#29:**

- [x] `propose_issue` and `propose_update` each have two cases: a repo the channel doesn't feed, and a label outside a repo's set list. Each returns `isError` with the policy's reason, through the real MCP server. An unknown channel never gets a run or a token, so it isn't reachable at the tool.
- [x] In those cases no proposal is stored, so no card is due, and nothing reaches Slack: the card sync loop draws nothing.

**#34:**

- [x] A non-approver who presses Approve gets an ephemeral refusal (#151).
- [x] Reaction approval is off by default and configurable. `[approval] reactions = false` stays the switch, and a new `reaction = "white_check_mark"` names the emoji. The name is validated as a Slack emoji name, and it can't be a keyword reaction such as `no_entry_sign`.
- [x] With reactions on, the configured reaction on a pending proposal's card counts as Approve. It runs every check #151 runs:
  - this workspace's team for the event, and the person's team through `users.info`
  - no guests or external users
  - the proposal's own card in its own channel
  - the approver list, and authorship from Slack
  - the hash lock
- [x] With reactions off, or with another emoji, the reaction is ignored silently, and nothing changes.
- [x] It's tested through the real Bolt app, with `reaction_added` events.

## Design

**The hash of the card as shown.** A reaction carries no hash, unlike the button. The card shows the stored draft only once `card_revision == revision`, which is `not record.card_due` (#152). So a reaction approves only when the card is up to date and the proposal is pending, locking `content_hash(record.proposal)`. A card that's behind gets an ephemeral reply, "This card is being updated; react again in a moment", and nothing changes.

**Too long to approve.** A card whose draft was too long to show in full has no Approve button, so a reaction mustn't approve it either. `cards.approvable(record, context)` is factored out of `render_card` and uses the same layout rule. The reaction path checks it with `card_context`, the context the card was drawn with.

**Finding the proposal.** A new `Store.proposal_for_card(channel_id, card_ts)` returns the proposal whose card is that message, in both stores. It's a lookup by existing columns, so there's no migration.

**Wiring.** `ApprovalHandler.on_reaction(event)` builds the same click a button press would: user, channel, the card's ts, and the content hash as the value. It then runs the person check, followed by `_approve`, so the approver, authorship and hash rules are the button's own code. The orchestrator's `note()` passes accepted reactions to it after the keyword handler. A reaction event carries no team for the payload or the person, so the payload team check is skipped. JotFlow still needs its own team from `auth.test`, and `users.info` decides `external`. Ingest only accepts events from JotFlow's own Socket Mode connection, in configured channels.

## Risks and open questions

- **The policy key** was proposed to the architect, with no objection so far. It keeps existing files valid.
- **Duplicate deliveries.** Ingest drops a repeat of the same person, message and emoji as a redelivery (#8). A test shows a second identical reaction is dropped, with no second run. A different approver's later reaction gets "already approved".
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [x] #29 tool-level tests
- [x] Policy key, writer, and example file
- [x] `proposal_for_card` in both stores, with contract tests
- [x] `cards.approvable`, factored out with no rendering change
- [x] `on_reaction`, and its orchestrator wiring
- [x] Real-app tests
- [x] Docs: architecture's approval section and `jotflow.example.toml`
- [x] `make check` passes

## Validation

`make check` is green, with 2018 tests.

- **#29:** through the real MCP server, `propose_issue` and `propose_update`, with a repo outside the channel or a label outside a set list, each return `isError` with the policy reason. No proposal is stored, no card is due, the real `CardSync` draws nothing, and nothing is posted.
- **#34:** through a real Bolt app and the real ingest filter, with `reaction_added` events:
  - a shown card approved by reaction;
  - reactions off, another emoji, and another message changing nothing, with no reply;
  - a card not yet redrawn refused;
  - a draft too long for the card refused;
  - a non-approver, the first message's author, and an external user refused, then a listed approver approving;
  - an identical repeat dropped;
  - a guest refused.
- Unit tests cover a malformed reaction, a settled card, a missing own team, the policy key's validation, and that keyword reactions and the reserved list stay in step.

## Handoff notes

Worktree `~/jotflow-work/wt-security-m4`, branch `feat/policy-tests-reaction-approval`.
