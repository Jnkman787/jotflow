# Clear drafted issue content once a proposal is final

## Links and status

- Issue: #102
- Also serves: #11 (the store), #96
- Draft PR: #103
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18 after rebasing onto main with #96
- Next safe action: review

## Objective and done-when

The store keeps the agent's drafted issue title and body on each proposal, because approval locks exactly what the card showed (D7) and the execution run needs those fields. A draft can quote Slack text verbatim. A proposal that is rejected, superseded, or abandoned after failing is never published, and an applied one already lives on GitHub. So once a proposal is final, its drafted text is cleared, and the audit trail stays.

Done when:

- [x] Moving to applied, rejected, or superseded clears the drafted title, body, and comment in the same transaction, in both stores, covered by the shared contract suite
- [x] A failed proposal keeps its draft for a retry; an explicit `clear_draft` clears it, after which failed to approved is refused
- [x] After clearing, the content hash, approver, approval time, state, result and issue URL, Slack pointers, participants, and non-text fields (repo, kind, labels, assignees, issue number, size, topic) stay readable, and `draft_cleared_at` records when
- [x] architecture.md Data and privacy states the retention rule
- [x] `make check` passes, including the coverage floor

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
- Migration numbering with #53: this PR takes version 2; whichever of the two merges second renumbers. The architect has been told.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Model field and `cleared_draft`
- [x] Migration
- [x] Clearing on final transitions and in `supersede_pending`, in both stores
- [x] `clear_draft` and the refused retry
- [x] Contract tests
- [x] architecture.md retention rule
- [x] CodeRabbit once: no findings
- [x] `make check` passes
- [x] Rebase onto main after #96 merges
- [x] Mark the PR ready for review

## Validation

- Contract tests, run against both stores: applied, rejected, and superseded drafts are cleared with the hash, approver, approval time, result, participants, and non-text fields intact; an update draft loses only its body and comment; a failed draft stays until `clear_draft`, after which a retry raises `DraftClearedError` and `try_transition` returns None; a live draft (pending, approved, executing, reconciling) cannot be cleared; `clear_draft` is a no-op once final.
- `secure_delete`: a SQLite test writes a draft, rejects it, closes the store, and finds no copy of the text in any file. With `secure_delete` off, the same steps leave the text in the main database file, so the test is meaningful.
- CodeRabbit: completed with no findings.
- Folded in after review: `clean_title` unescapes until the text stops changing, at most 8 passes, instead of twice (security's #96 nit, at the team lead's request); anything escaped deeper loses its `&` as markup. A test pins migration versions to 1..N with no gaps or duplicates (architect's request), since #53 also adds a migration and whichever merges second renumbers.
- Schema test: SQLite splices an added column into the stored CREATE TABLE after the last column, mid-line, and a `--` comment in the ALTER would swallow the closing parenthesis. Added columns therefore use `/* */` comments, documented in `migrations.py`, and the schema test now tokenises the column list and gives a `--` comment to the column that starts its line. Tests cover added columns, a bad comment on one, and the spliced proposals table.

## Handoff notes

Rebased onto main after #96 merged.
