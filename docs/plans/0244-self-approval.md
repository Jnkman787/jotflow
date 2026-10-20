# Listed approvers may approve their own proposals

## Links and status

- Issue: #244
- Draft PR: #245
- Status: ready for review
- Last verified: `make check`, 2026-10-20
- Next safe action: review by the reviewer and security

## Objective and done-when

The maintainer decided on 2026-10-20 that a person trusted to approve may approve their own ideas.

- [x] With approvers on and `require_second_approver` unset or false, a listed approver can approve their own proposal, by button or by reaction (tested).
- [x] With `require_second_approver = true`, today's refusal stands, for button and reaction (tested).
- [x] `jotflow check` and `jotflow init` handle the new key.
- [x] D7 in docs/roadmap.md, the approval table in docs/architecture.md, the threat model's "Approve their own proposal" row, the help text and the CHANGELOG all state the new default. The threat model records the trade-off: under the default, one listed approver acting alone can approve their own idea.

**Out of scope:**
- Approvers off, which stays unchanged: anyone in the channel may approve, including the poster.
- Guests and external users, who still never approve.
- The D7 integrity guarantees.

## Design

**The key.** `ApprovalPolicy.require_second_approver: bool = False`. In `_approve`, the author check runs only when approvers are on and this key is true. The check is unchanged: Slack-confirmed authors of the thread's first message and of the cited sources. The reaction path already calls `_approve`, so the same change covers it. `OWN_PROPOSAL` keeps its text.

**With approvers off, the key would do nothing**, as the issue says. That could mislead an admin who sets it expecting a two-person rule. Security asked that the policy refuse it, or that check warn. It's refused when the policy loads: "require_second_approver needs [approval] approvers; list them, or remove require_second_approver". That fails closed, and `jotflow check` reports it as a configuration problem.

**`jotflow init`.** Setup never asks for approvers, which are set by hand in `jotflow.toml`, so there's no approvers step where it could mention the key. Setup does rewrite `jotflow.toml` through `policy_writer` when channels change. The writer now renders `require_second_approver`, so a rewrite keeps the setting. `examples/jotflow.example.toml` documents the key, and the writer's round-trip test covers it.

**Help.** With approvers on, the propose-mode line becomes one of these:
- "Only the channel's approvers can approve, including proposals drawn from their own messages."
- "Only the channel's approvers can approve, and never a proposal drawn from their own messages."

**Docs.**
- D7 changes "and no one approves their own proposal" to the new default, naming the key.
- architecture.md's table gets a row for approvers on with the key.
- The threat model uses the wording security agrees.
- The CHANGELOG gets a new `[Unreleased]` with a `### Changed` entry, since this changes a default.

## Risks and open questions

- An existing deployment with approvers on changes behaviour after upgrading: approvers can then approve their own proposals. The CHANGELOG entry says so and names the key that keeps the old rule.

## Progress checklist

- [x] Confirm the issue is ready
- [x] Security agrees the threat-model wording, with three changes
- [x] Commit this plan and open the draft PR
- [x] The key and its validator, `_approve`, help and the writer
- [x] Tests: button, reaction and Retry, with the key on and off; edit-then-approve; the validator through check; the writer; help
- [x] Docs and CHANGELOG
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` passes.
- Security agreed the threat-model wording, with three changes, all applied:
  - The accepted row names edit-then-approve (#160) and Retry.
  - The key without an approver list is refused.
- `_approve` keeps the approver-list check unconditional, and only the authorship clause depends on the key.

## Handoff notes

- A nice-to-have from security: when the approver also wrote a source, the card's "approved by" line could say so. That would need the author lookup even with the key off. It isn't in this PR.
