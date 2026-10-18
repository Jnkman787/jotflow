# Clear drafted issue content once a proposal is final

## Links and status

- Issue: #102
- Also serves: #11 (the store), #96
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written; branch stacked on #96 until it merges
- Next safe action: write the migration and the clearing rule in `src/jotflow/store/`

## Objective and done-when

The store keeps the agent's drafted issue title and body on each proposal, because approval locks exactly what the card showed (D7) and the execution run needs those fields. A draft can quote Slack text verbatim. A proposal that is rejected, superseded, or abandoned after failing is never published, and an applied one already lives on GitHub. So once a proposal is final, its drafted text is cleared, and the audit trail stays.

Done when:

- [ ] Moving to applied, rejected, or superseded clears the drafted title, body, and comment in the same transaction, in both stores, covered by the shared contract suite
- [ ] A failed proposal keeps its draft for a retry; an explicit `clear_draft` clears it, after which failed to approved is refused
- [ ] After clearing, the content hash, approver, approval time, state, result and issue URL, Slack pointers, participants, and non-text fields (repo, kind, labels, assignees, issue number, size, topic) stay readable, and `draft_cleared_at` records when
- [ ] architecture.md Data and privacy states the retention rule
- [ ] `make check` passes, including the coverage floor

**Out of scope:** expiring other data, such as the agent's error text in a failed result (#37). Clearing anything outside the proposals table.

## Design

Team lead's decision, 2026-10-18: clear applied, rejected, and superseded drafts at the transition. Clear a failed draft only by an explicit step, after which a retry is refused. Keep non-text fields and Slack pointers, add a cleared-at timestamp, and remove only the title, body, and comment.

- `models.py`: `ProposalRecord.draft_cleared_at: AwareDatetime | None`. A new `cleared_draft(proposal)` returns a copy with the free text gone. An issue draft's `title` becomes `"(cleared)"`, because the model requires a non-empty title the agent fills in, and its `body` becomes empty. An update draft's `changes.body` and `changes.comment` become `None`. Everything else is kept.
- Migration: one new column, `proposals.draft_cleared_at`. Its version number follows whichever migration lands first, this one or #53's runs table; agreed with the architect.
- `base.py`: `FINAL_STATES = {applied, rejected, superseded}`. `plan_transition` marks the draft for clearing when the target is final. Failed to approved raises `DraftClearedError`, a `TransitionError`, when the draft is gone, so `try_transition` returns None for it too.
- `Store.clear_draft(proposal_id)`: allowed in failed or a final state, and a no-op if already cleared. Anywhere else it raises `TransitionError`, because a pending or in-flight proposal still needs its draft.
- `supersede_pending` clears the superseded drafts in the same transaction.
- The content hash stays the hash of the original draft. So an applied issue can still be matched against what was approved, while the text itself is gone from the store.

## Risks and open questions

- `"(cleared)"` as a title placeholder is visible to any reader of a cleared record. The alternative, making `title` optional in the agent-facing model, would loosen the schema the agent fills in, so the placeholder plus `draft_cleared_at` is the safer choice.
- Migration numbering with #53, as above.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Model field and `cleared_draft`
- [ ] Migration
- [ ] Clearing on final transitions and in `supersede_pending`, in both stores
- [ ] `clear_draft` and the refused retry
- [ ] Contract tests
- [ ] architecture.md retention rule
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Rebase onto main after #96 merges
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

Stacked on #96's branch until it merges; then rebase onto main with `git rebase --onto origin/main origin/feat/sqlite-store`.
