# Use what the poster names: repo, board and assignee

## Links and status

- Issue: #331
- Draft PR: #351, for the plan. Built in this PR, which brings the plan in
- Status: ready for review. The lead approved the plan and the bytes on 2026-10-29. Security approved `named_in_text` with conditions, and the final match rule below went back to security for its ok
- Last verified: 2026-10-29, against main
- Next safe action: review, and security's ok on the final match rule

## Objective and done-when

A poster who writes "Assign to Sam and put it on the customer support board" in the web app's channel shouldn't be asked which repo. In the review, 4 of 15 posts were directives like this.

From #331:

- [ ] A repo or board the poster names is used without asking, by its name, nickname or alias. The agent asks only when the name is ambiguous
- [ ] An assignee named in plain text, without an @-mention, is handled, by a rule agreed with security that builds on #325's trust rules
- [ ] Configured repos can have aliases
- [ ] A vague answer to the repo question gets one follow-up question, not a guess
- [ ] Replay fixtures cover each case

**Out of scope:** assigning someone who isn't in the thread and isn't an owner hint, which needs a directory search by name. Typo correction beyond a name that matches one repo once case and punctuation are ignored. Setting a board's fields.

## Design

### 1. A named repo

- **Aliases:** `[[channels.<id>.repos]]` gains an optional `aliases = ["ovens", "proofer"]`. Each is a `Nickname`-shaped string, and the channel's existing uniqueness check covers it, across nicknames, scope aliases and boards. The policy writer round-trips it, and the packet's `RepoBrief` carries it.
- **drafting.md, step 4:** "If the poster names one of `channel.repos` outright, by owner/name, nickname or alias, use it without asking. Ask only when the name fits more than one."
- **A vague answer, step 8:** "If a reply to your repo question names none of `channel.repos` ('whichever affects the ovens most'), ask once more, listing their nicknames. Never guess from a vague answer."
- **No code checks the agent's choice beyond today's.** The repo must be one of the channel's, and the card shows it with the repo menu.

### 2. A named board

- **drafting.md, at `boards`:** "A board the poster names, by its nickname or plainly by its description ('the customer support board'), is proposed with that as its reason."
- **The checks stay as they are.** Boards must be the channel's, the card shows them, and #327 lets a person change them.

### 3. A plain-text assignee: needs security's ok

"Assign to Sam" names Sam without an @-mention, so #325's `named_owner` doesn't apply, since it's checked against Slack's mentions. My proposal is a new reason, `named_in_text`. JotFlow checks every part of it:

- **Its `source_ts`** is a message in the run's packet.
- **The login** belongs to someone in the packet's `people`, who wrote in or was mentioned in the thread, or to one of the repo's `owner_hints`. Nobody outside those is reachable by a name.
- **The name:** that person's Slack display name, or real name, appears in the source message as a whole word, ignoring case, in Slack's text with mentions stripped.
- **Uniqueness:** no other person in `people` or `owner_hints` has a display or real name that also matches a word in that message. With two Sams in reach, it's refused, and the agent asks.
- **Trust:** the link must be trusted, verified or admin-linked, as for `named_owner` and `owner_hint` since #325. An untrusted one is left off and reported in `left_off_assignees`, so the agent asks.
- **On the card:** "named in the thread", JotFlow's words, as for the other reasons.

drafting.md: "`named_in_text`: a message names them as the owner in words ('assign to Sam') and they're in `people` or `owner_hints`. If two people could match, ask."

The names come from Slack's `users.info`, through the packet's lookup, never from the agent. That lookup is already cached per run.

### 4. Fixtures

- `repo-named.json`: "This is for the api: …", which names the api by nickname with two repos configured. There's one proposal for `acme-co/api` and no question.
- `repo-alias.json`: the same, through an alias.
- `vague-repo-answer.json`: the agent asks which repo, and the reply is vague. The agent asks once more, listing nicknames, and proposes nothing.
- `board-named.json`: "put it on the support board", which proposes that board.
- `assign-by-name.json`:
  - "Assign to Bob", where Bob replied in the thread and is admin-linked, assigns `acme-bob` as `named_in_text`.
  - The same with two Bobs in reach is refused, and the agent asks.
  - An unverified Bob is left off.

## Decisions

**The match rule,** from security, the reviewer and the lead, on 2026-10-29. It's implemented in `jotflow/name_match.py`:
- Only the packet's people, recorded by `get_work` in the ledger, and the repo's `owner_hints` can be named.
- A person matches by their full Slack display name, or the first word of their real name, read from `users.info` at draft time, as whole words in the source message with mentions stripped.
- The match is case-sensitive, needs at least 3 letters, and never matches a word on the fixed stoplist, which includes me, it, may, will, grant and the like.
- Exactly one candidate must match, or the reason is refused and the agent asks. The link must be trusted, or the person is left off.
- The card shows "named in the thread ('Sam')". A later rename shows on a redrawn card, since names are read at draft time.

**A vague repo answer:** after a second vague answer the agent proposes nothing and leaves the topic for a person.

**Bytes:** drafting.md grows by 578, from 11,358 to 11,936, with each rule pinned.

## Risks and open questions

1. **Security: is `named_in_text` acceptable as above?** The main risk is matching the wrong person by name. Limiting it to people already in the thread or an owner hint, requiring a unique match and a trusted link, and having an approver approve the card keeps it narrow. The alternative is to make the agent ask for an @-mention every time. That's safer, but it's exactly what annoyed people.
2. **The lead: drafting.md bytes.** I estimate about 600 bytes over the five rules, under the 14,000 cap, with each new rule pinned.
3. **Ambiguous display names:** Slack display names aren't unique. The uniqueness check runs only within the thread's people and owner hints, which is the only set the agent can reach anyway.

## Progress checklist

- [x] Confirm the issue is ready
- [ ] Commit this plan and open the draft PR
- [x] Security and the lead decide open questions 1 and 2
- [x] Aliases: policy, the writer, the packet and uniqueness, and `re:`
- [x] `named_in_text`: the model, the write tools' check, the card's words, and the packet's names
- [x] drafting.md steps 4, 6, 7 and 8, with pins
- [x] Fixtures, plus unit tests for the alias policy and the name match
- [x] Docs: usage.md, architecture.md, the threat model's assignee row, the example config and the CHANGELOG
- [ ] `make check`, then mark ready

## Validation

- These fixtures replay to their expected outcomes, in the scripted replay and over real MCP: `repo-named.json`, `repo-alias.json`, `board-named.json`, `vague-repo-answer.json`, `assign-by-name.json` and `common-word-name.json`.
- The tests are `tests/test_name_match.py`, the `named_in_text` tests in `tests/test_assignee_reasons.py`, and the alias tests in `tests/test_policy.py` and `tests/test_keyword_handler.py`.
- Replay now advances its clock between a thread's scripted runs, as a live quiet window does, so a second question in a later run isn't refused.
- `make check` passes.

## Handoff notes

All examples are fictional: `acme-co/web`, `acme-co/api`, Bob and Sam.
