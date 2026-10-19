# Show an overruled keyword hint on the proposal card

## Links and status

- Issue: #147
- Also serves: #144 (adds `hint_overruled`), #23, D10, D7
- Draft PR: #149, based on `feat/drafting-prompt` until #144 merges
- Status: implementing
- Last verified: plan written on 2026-10-19; the branch is stacked on #144's `feat/drafting-prompt`
- Next safe action: once #144 merges, rebase onto main and mark #149 ready

## Objective and done-when

When the drafting agent overrules an author's `idea:` or `bug:` hint, the proposal carries the agent's one-line reason in `hint_overruled`. The card shows it, so people see that the hint was overruled and why before they approve.

Done when:

- [x] A card for a proposal with `hint_overruled` shows "Hint overruled: <reason>" as `plain_text`, so nothing in it can ping or link
- [x] A card without it looks exactly as it does today
- [x] Card tests cover both cases, for a new issue and for an update
- [x] `make check` passes, including the coverage floor

**Out of scope:** what the agent writes there, and when, which is #144's prompt work.

## Design

- In `cards.py`, `_hint_blocks(draft)` returns a context block with `Hint overruled: <reason>`, or nothing when the field is null. It goes just after the confidence line, for both `IssueProposal` and `UpdateProposal`. That is where people read how sure the agent is.
- It uses `_approved_context`, like the other approved lines. The approval hash covers `hint_overruled`, so if the line ever had to be shortened, the card could not be approved. At 200 characters, the field is far below Slack's context limit, so in practice it never is.
- The line stays when a proposal's draft is cleared after approval or rejection. It is short, and it explains the outcome.
- The reason is untrusted agent text. It goes through `plain()`, as every agent string does, and the existing "agent text only ever appears as plain text" test is extended to cover it.
- No new block budget is needed in practice. The layout fallbacks in `render_card` already count every block.

## Risks and open questions

- #144 is open. This branch builds on it and will be rebased onto main once #144 merges, so it must merge after #144.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Render the line for both proposal kinds
- [x] Tests: with and without, both kinds, plain text only
- [x] Docs: architecture's card description, if it lists the card's lines
- [x] CodeRabbit once: skipped, the free CLI allowance was rate-limited
- [x] `make check` passes
- [ ] Rebase onto main after #144 merges, then mark the PR ready for review

## Validation

- `make check` passes.
- `tests/test_cards.py` shows the line just after the confidence line, for a new issue and for an update. A card without the field is unchanged, and adding the field adds exactly one block. An injection string stays in `plain_text`, never `mrkdwn`. The Approve button's hash covers the reason.

## Handoff notes

None yet.
