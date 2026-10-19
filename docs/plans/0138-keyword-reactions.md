# Keyword reactions: undo a skip, and the idea and bug hints

## Links and status

- Issue: #138, and #139 in the same PR
- Also serves: #56 (keywords and the skip reaction), D10, D7, #154 (`reactions.py` and the policy's collision check)
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-19, no code yet
- Next safe action: the store's reaction marks

## Objective and done-when

Every keyword with a reaction should work both ways. You add the reaction to mean the keyword, and remove it to take it back. Today only `:no_entry_sign:` exists, it means skip, and it can't be taken back. After this:

- removing the `:no_entry_sign:` that skipped a message undoes the skip (#138)
- `:bulb:` and `:bug:` mean `idea:` and `bug:`, and reach the agent as hints (#139)

Done when, for #138:

- [ ] The manifest subscribes to `reaction_removed`, and ingest accepts it with the same guest and external-user rules as `reaction_added`
- [ ] Removing the `:no_entry_sign:` that skipped a message clears the person's chatter mark, so the message reaches drafting again
- [ ] Only the person who reacted, the author, or an approver can undo it, matching who could skip
- [ ] The thread note says the reaction can be removed to undo
- [ ] The install docs say the scopes changed and a reinstall is needed

Done when, for #139:

- [ ] Reactions for idea and bug are chosen and listed in `reactions.KEYWORD_REACTIONS`, the single source, which `keywords.REACTIONS` re-exports, so the policy's collision check covers them
- [ ] The hint from a reaction is stored per message, and the work packet passes it to the agent like a text hint
- [ ] A text keyword wins over a reaction on the same message
- [ ] Guest and external reactions are ignored, as for skip
- [ ] `/jotflow help` lists the new reactions

Both: `make check` passes, including the coverage floor.

## Design

- **Reaction marks, in the store.** Migration 11 adds `reaction_marks (channel_id, ts, user_id, keyword, marked_at)`, with a primary key across the first four. A row means this person's reaction stands for this keyword on this message. Both stores get `add_reaction_mark`, `remove_reaction_mark`, and `reaction_marks(channel_id, ts)`.
  - Recording who reacted is what makes undo safe. Removing one person's reaction undoes the skip only when no one else's skip reaction remains.
  - A skip that came from the `skip:` text has no row, so removing a reaction never undoes it.
- **Skip and undo (#138).** On `reaction_added :no_entry_sign:`, as today, the message is marked as the person's chatter and their mark is recorded. On `reaction_removed :no_entry_sign:`:
  - Their mark is removed.
  - If no skip mark is left, the chatter mark is cleared, through a new `Store.unmark_chatter(ref)` that removes only a person's chatter mark. The thread is armed again, so the message reaches drafting.
  - Who may undo: Slack lets only the person who added a reaction remove it. So the undo is always by the person who reacted, and that person passed the skip rule when they reacted. An author or approver who disagrees asks in the thread, as the note says. The docs state this plainly, rather than claiming more.
- **Hints (#139).** `:bulb:` means idea and `:bug:` means bug, added to `reactions.KEYWORD_REACTIONS`.
  - A reaction records the person's mark under the same rules as skip: guests and external users are ignored, and with approvers on, only the author or an approver may react on someone else's message.
  - The work packet asks the store for a message's marks. When the text has no keyword, the earliest idea or bug mark becomes the hint. A text keyword always wins.
  - Removing the reaction removes the mark.
  - JotFlow confirms a hint reaction as it confirms a keyword, with `white_check_mark`.
- **Ingest.** `reaction_removed` becomes an accepted kind, `"reaction_removed"`, with the bot's own reactions ignored as for `reaction_added`. The debouncer still ignores both. The orchestrator routes it to the keyword handler, and in a paused channel it reads nothing, as for every event (#28).
- **The manifest** adds `reaction_removed`, which needs `reactions:read`, already granted. The README's install section and the manifest notes say a reinstall is needed. `/jotflow help` and the README help block list all three reactions, from `reactions.py`.
- **The thread note** for someone else's skip now says "<who> can remove the reaction to undo it, or say so in this thread".

## Risks and open questions

- **Reinstall.** Teams must reinstall the app for `reaction_removed`. Until they do, undo and hint removal silently do nothing, and adding still works. The install docs and the release notes say so.
- `:bulb:` and `:bug:` are ordinary emoji people may use casually. A hint is only a hint, and the agent may overrule it, so a stray one costs little. The confirmation reaction shows the poster what JotFlow understood.
- **Migration numbers.** 11 is free today.

## Progress checklist

- [x] Confirm the issues are ready: bounded objectives, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Store: migration 11, reaction marks, `unmark_chatter`, both stores, contract tests
- [ ] Ingest and manifest: `reaction_removed`
- [ ] Keyword handler: skip marks, undo, hint marks, and the note
- [ ] Packet: a reaction hint when the text has none
- [ ] Help, README block, install docs
- [ ] Tests through the real Bolt app and orchestrator
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
