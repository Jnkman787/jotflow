# Keyword reactions: undo a skip, and the idea and bug hints

## Links and status

- Issue: #138, and #139 in the same PR
- Also serves: #56 (keywords and the skip reaction), D10, D7, #154 (`reactions.py` and the policy's collision check)
- Draft PR: #170
- Status: implementing
- Last verified: 2026-10-19, `make check` passes with the code below
- Next safe action: CodeRabbit, then ready for review

## Objective and done-when

Every keyword with a reaction should work both ways. You add the reaction to mean the keyword, and remove it to take it back. Today only `:no_entry_sign:` exists, it means skip, and it can't be taken back. After this:

- removing the `:no_entry_sign:` that skipped a message undoes the skip (#138)
- `:bulb:` and `:bug:` mean `idea:` and `bug:`, and reach the agent as hints (#139)

Done when, for #138:

- [x] The manifest subscribes to `reaction_removed`, and ingest accepts it with the same guest and external-user rules as `reaction_added`
- [x] Removing the `:no_entry_sign:` that skipped a message clears the person's chatter mark, so the message reaches drafting again
- [x] Only the person who reacted, the author, or an approver can undo it, matching who could skip
- [x] The thread note says the reaction can be removed to undo
- [x] The install docs say the scopes changed and a reinstall is needed

Done when, for #139:

- [x] Reactions for idea and bug are chosen and listed in `reactions.KEYWORD_REACTIONS`, the single source, which `keywords.REACTIONS` re-exports, so the policy's collision check covers them
- [x] The hint from a reaction is stored per message, and the work packet passes it to the agent like a text hint
- [x] A text keyword wins over a reaction on the same message
- [x] Guest and external reactions are ignored, as for skip
- [x] `/jotflow help` lists the new reactions

Both: `make check` passes, including the coverage floor.

## Design

- **Reaction marks, in the store.** Migration 11 adds `reaction_marks (channel_id, ts, user_id, keyword, marked_at)`, with a primary key across the first four. A row means this person's reaction stands for this keyword on this message. Both stores get `add_reaction_mark`, `remove_reaction_mark`, and `reaction_marks(channel_id, ts)`.
  - Recording who reacted is what makes undo safe. Removing one person's reaction undoes the skip only when no one else's skip reaction remains.
  - A skip that came from the `skip:` text leaves a mark under the reserved name `text`, so removing a reaction never undoes it.
- **Skip and undo (#138).** On `reaction_added :no_entry_sign:`, as today, the message is marked as the person's chatter and their mark is recorded. On `reaction_removed :no_entry_sign:`:
  - Their mark is removed.
  - If no skip mark is left, the chatter mark is cleared, through a new `Store.unmark_chatter(ref)` that removes only a person's chatter mark. The thread is armed again, so the message reaches drafting.
  - Who may undo: Slack lets only the person who added a reaction remove it. So the undo is always by the person who reacted, and that person passed the skip rule when they reacted. An author or approver who disagrees asks in the thread, as the note says. The docs state this plainly, rather than claiming more.
- **Hints (#139).** `:bulb:` means idea and `:bug:` means bug, added to `reactions.KEYWORD_REACTIONS`.
  - A reaction records the person's mark under the same rules as skip: guests and external users are ignored, and with approvers on, only the author or an approver may react on someone else's message.
  - The work packet asks the store for a message's marks. When the text has no keyword, the earliest idea or bug mark becomes the hint. A text keyword always wins.
  - Removing the reaction removes the mark.
  - JotFlow confirms a hint reaction as it confirms a keyword, with `white_check_mark`.
- **Ingest.** `reaction_removed` becomes an accepted kind, `"reaction_removed"`, with the bot's own reactions ignored as for `reaction_added`. Accepting an add forgets an earlier removal's dedup key, and the reverse, so reacting again after a removal is not mistaken for a redelivery. The debouncer still ignores both. The orchestrator routes it to the keyword handler, and in a paused channel it reads nothing, as for every event (#28).
- **The manifest** adds `reaction_removed`, which needs `reactions:read`, already granted. The README's install section and the manifest notes say a reinstall is needed. `/jotflow help` and the README help block list all three reactions, from `reactions.py`.
- **The thread note** for someone else's skip now says "<who> can remove the reaction to undo it, or say so in this thread".

## Risks and open questions

- A reply skipped by reaction before JotFlow stored it has no known thread. Undoing it arms the message's own ts, which is right for a top-level message. For such a reply, the thread drafts again with its next message instead.
- JotFlow's confirmation reaction stays on a message after its reaction is removed. Removing it too is a possible follow-up.

- **Reinstall.** Teams must reinstall the app for `reaction_removed`. Until they do, undo and hint removal silently do nothing, and adding still works. The install docs and the release notes say so.
- `:bulb:` and `:bug:` are ordinary emoji people may use casually. A hint is only a hint, and the agent may overrule it, so a stray one costs little. The confirmation reaction shows the poster what JotFlow understood.
- **Migration numbers.** 11 is free today.

## Progress checklist

- [x] Confirm the issues are ready: bounded objectives, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Store: migration 11, reaction marks, `unmark_chatter`, both stores, contract tests
- [x] Ingest and manifest: `reaction_removed`
- [x] Keyword handler: skip marks, undo, hint marks, and the note
- [x] Packet: a reaction hint when the text has none
- [x] Help, README block, install docs
- [x] Tests through the real Bolt app and orchestrator
- [ ] CodeRabbit once, or skipped
- [x] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- `make check` passes.
- The store contract tests cover, on both stores: one mark per person and keyword, oldest first; removing only that mark; refusing a keyword no reaction stands for; and `unmark_chatter` removing only a person's chatter mark.
- `tests/test_keyword_handler.py` covers:
  - removing the skip reaction undoes the skip and names the thread
  - another person's skip reaction keeps the skip
  - a `skip:` text is never undone, and a guest's removal changes nothing
  - idea and bug reactions are hints that are confirmed but not chatter, and are removed again
  - hints follow the guest and approver rules
  - the policy refuses a keyword reaction as the issue or approval reaction
- `tests/test_ingest.py` shows `reaction_removed` accepted, re-adding after a removal not taken for a redelivery, and the bot's own removal ignored.
- `tests/test_packet.py` shows the earliest idea or bug reaction becoming the hint, a text keyword winning, and skip being no hint.
- `tests/test_service.py`, through a real Bolt app, shows a skip reaction keeping the thread from drafting, and removing it clearing the skip and arming the thread again.
- The manifest tests and the checked-in manifest include `reaction_removed`. The README help block is regenerated, and its drift test passes.

## Handoff notes

None yet.
