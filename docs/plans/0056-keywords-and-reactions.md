# Intent keywords and reactions

## Links and status

- Issue: #56
- Also serves: D10, D11, D7, #20 (the keyword module), #19 (the debouncer's join), #129 (replay's `re:` exception)
- Draft PR: #135
- Status: ready for review
- Last verified: 2026-10-18, `make check` passes on the branch rebased onto main
- Next safe action: review; wiring the handler into `jotflow run` follows with #55

## Objective and done-when

People can steer JotFlow with a keyword at the start of a message, or the equivalent reaction (D10). `idea:` and `bug:` reach the agent as hints; `re:` and `skip:` JotFlow applies itself, without an agent run, and confirms with a reaction.

Done when:

- [x] "Skip button is broken" is not treated as a keyword; typos are tolerated only on keywords of four or more letters, colon still required
- [x] `re:` outside the channel's repos is refused with a reason; `re: 42` works when the channel has one repo and `re: web#42` otherwise (D11)
- [x] Guest and external-user reactions are ignored
- [x] No keyword can skip approval or change the mode
- [x] `/jotflow help` lists the keywords
- [x] A `skip:` reply ends up as person-marked chatter, and an `re:` reply is linked to the named issue's topic, whichever order the keyword handler and the debouncer's join run in (reviewer, from #127)
- [x] Replay applies `re:` and `skip:` through the same handler and no longer sets `re:` messages aside in its topic comparison (lead, from #132)
- [x] `make check` passes, including the coverage floor

**Out of scope, as follow-ups:**
- Wiring the handler into `jotflow run` comes with #55, which owns opening the store in `serve()`. This PR registers only `/jotflow help`, which needs no store.
- Undoing someone else's skip by removing the reaction needs the `reaction_removed` event, a manifest change that teams would reinstall for. For now the visible note says how to ask for an undo. Follow-up: #138.
- `idea:` and `bug:` given as reactions have no effect yet: the hint reaches the agent from the message text, and a reaction has nowhere to be stored. The confirmation reaction says the keyword was understood only when it was acted on. Follow-up: #139.
- The rest of #80's help surfaces: the pinned guide, the newcomer tip, and near-miss nudges. `help_text()` is the one source #80 builds on.

## Design

- **`keywords.py`**, extended, never a second parser:
  - `split_keyword` keeps its signature and result, `(keyword, rest)`. It gains D10's typo tolerance: the first word, with its colon, may be one wrong letter or one swap of two neighbouring letters away from a keyword of four or more letters (`idea`, `skip`). The word must keep its length, so `ideas:` stays ordinary text, as #20's pinned test requires. `re` and `bug` must be exact.
  - `parse_reference(text, channel) -> IssueReference`, or `ReferenceError` with a reason. It accepts `42` when the channel has one repo, and `nickname#42` or `owner/name#42` otherwise. A repo the channel does not list, or a missing or zero number, is refused.
  - `REACTIONS`: `no_entry_sign` means skip. `re:` has no reaction, because it needs an issue number.
- **`KeywordHandler(store, policy, slack)`**, in `orchestrator/keyword_handler.py`:
  - `on_message(event)`, for an accepted message:
    - `skip:` calls `mark_chatter(by="person")`; on a top-level message, the packet already leaves the whole thread out.
    - `re:` resolves the reference, finds the channel's topic for that issue, or creates one titled `web#42` and links it to the issue, then calls `link_message(by="person")`.
    - A refused reference gets a thread reply with the reason.
    - A recognised keyword gets a confirmation reaction, `white_check_mark`.
  - `on_reaction(event)`, for an accepted `no_entry_sign` reaction:
    - It is ignored from guests and external users, checked through `users.info`.
    - Approvers off: anyone may skip, and someone else's message gets a visible thread note naming the person.
    - Approvers on: only the author or an approver may.
    - The message is then marked as person-made chatter.
  - **Order with the debouncer.** The handler runs before `Debouncer.note`. A person's decision is already stored when the debouncer's join runs, and the join leaves placed messages alone. If a skip reaction arrives after the join, `mark_chatter(by="person")` overrides the agent-level link, because a person beats the agent. Tests cover both orders.
  - The `Slack` protocol for reactions, replies, and user lookups, with a Bolt implementation, so tests use a fake.
  - The handler never calls `transition`, `set_channel_mode`, or anything that approves. A test checks that with a recording store.
- **`/jotflow help`**: `help_text()` lists the keywords and the skip reaction. `register_commands(app)` answers privately, with an ephemeral response.
- **Replay**: runs the same handler over accepted messages before the scripted runs, with a fake Slack, and drops its `re:` set-aside.

## Risks and open questions

- Typo tolerance inside `split_keyword` changes what the live packet strips and hints, so the packet and the handler agree. I asked the architect, who wrote the module, and kept insertions and deletions out so #20's tests hold unchanged.
- A real word one letter away, like `ship:` or `skin:`, becomes `skip`. D10 accepts that trade for four-letter keywords, and the confirmation reaction shows the poster what happened.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `keywords.py`: typo tolerance, `parse_reference`, `REACTIONS`
- [x] `KeywordHandler` and its `Slack` protocol
- [x] `/jotflow help`
- [x] Replay through the handler; drop the `re:` set-aside
- [x] Tests, both orders with the debouncer
- [x] Docs: architecture Intent keywords, AGENTS.md layout, README "Using it"
- [x] Follow-up issues: undo by removing a reaction; hint reactions
- [x] CodeRabbit once: three findings, all fixed (typo example, unknown reaction author, reaction wording)
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` passes: end-of-line check, ruff lint and format, mypy strict, and the tests with the coverage floor.
- `tests/test_keyword_handler.py` runs every store-backed case on MemoryStore and SqliteStore. It covers typos, reference resolution and refusals, both orders with `Debouncer.note` for `skip:` and `re:`, a skip reaction after the join, the approver rule, guests and external users, and a recording store that shows no keyword approves or changes the mode.
- `jotflow replay` on the keywords fixture matches with no set-aside. `tests/test_replay.py` shows an `re:` naming the wrong repo now fails the topics check.

## Handoff notes

- #55 wires `KeywordHandler` into `jotflow run`: call `on_message` before `Debouncer.note`, and `on_reaction` for accepted reactions. `BoltKeywordSlack` needs the workspace's team id.
- The fixture validator in `testing/threads.py` still leaves `re:` messages out when it checks what the script links. That is correct: the agent never links them, the handler does.
