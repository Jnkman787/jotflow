# Block Kit proposal card

## Links and status

- Issue: #26
- Also serves: #27 (Approve and Request Changes handle this card's buttons), #33, D7, D11
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: write `src/jotflow/orchestrator/cards.py`

## Objective and done-when

A pending proposal appears in its thread as a card that shows everything a person approves (D7). The same card is the summary after an auto-mode write, and it updates in place as the proposal moves through its states.

Done when:

- [ ] The card renders for create and update proposals
- [ ] It shows the full issue body (the #51 note supersedes "summarised"), the repo with a dropdown of the channel's repos (D11), labels, assignees, size, confidence, and source permalinks
- [ ] A public repo shows a warning, unknown visibility says it was not checked, and unverified identities are marked
- [ ] **Involved** (participants, by display name) is shown apart from **Assignees**, with a one-click action to make an involved person an assignee, and to remove an assignee
- [ ] Every agent-written string renders as escaped `plain_text`, never `mrkdwn`: title, body, labels, comment, assignee logins (#94 and #96 notes). Without a configured label list, labels are marked as not checked against one.
- [ ] The card updates in place: posting stores the card's message ts, and later states edit that message
- [ ] `make check` passes, including the coverage floor

**Out of scope:** what the buttons do (#27) and the dropdown's change (D11 handler), which only need the action ids and values this card carries. Looking up repo visibility, which needs GitHub access JotFlow does not have: the caller passes it in, from the agent's drafting run.

## Design

- `src/jotflow/orchestrator/cards.py`, pure functions:
  - `CardContext`: the channel's `ChannelPolicy`, display names by Slack user id, identity links by GitHub login, repo visibility (`public`, `private`, or unknown), and the policy's label rules.
  - `render_card(record, context) -> Card(blocks, text)`. `text` is a fallback for notifications and holds no agent text beyond the repo name.
- Layout, top to bottom:
  1. **Header:** "Proposed issue" or "Proposed update to web#42".
  2. **Repo:** a static select of the channel's repos, set to the proposal's (D11), and a warning line when the repo is public.
  3. **Title:** plain text.
  4. **Body:** plain text, in sections of at most 3000 characters, which is Slack's limit for one section. The message limit is 50 blocks, so a body too long to show in full gets a note in place of the Approve button. Approval must cover what was shown (D7).
  5. **Labels, size, confidence:** plain text. When the repo has no label list, the labels say "not checked against a list".
  6. **Assignees:** each GitHub login, marked "(unverified)" when the person's link is self-declared, with a Remove button. **Involved:** each participant by display name, with a "Make assignee" button when they have a linked login.
  7. **Sources:** permalinks to the Slack messages, only `https://*.slack.com/` links.
  8. **State:** pending shows Approve and Request Changes. Every other state shows a line instead: approved by someone or by auto mode, executing, reconciling, applied with the issue link, rejected, superseded, failed, or draft cleared.
- **Binding approval to what was shown.** The Approve button's value is `<proposal id>:<content hash of the draft as rendered>`. #27 passes the hash to `Store.transition(expected_hash=...)`, so a stale card cannot approve changed content (D7).
- **Escaping.** Agent text only ever goes into `plain_text` objects, which Slack never parses for mentions or links. `mrkdwn` is used only for text JotFlow builds itself, such as the approver's `<@U...>` from a validated id and the permalinks.
- **Updating in place.**
  - Migration 5 adds `proposals.card_ts`, with `Store.set_card_ts(proposal_id, ts)`, and `ProposalRecord.card_ts` reads it back.
  - `CardPublisher(store, poster).show(record, context)` posts a new card into the thread when there is no `card_ts`, and updates that message otherwise.
  - `CardPoster` is a small protocol, with `post(channel, thread_ts, blocks, text) -> ts` and `update(channel, ts, blocks, text)`. `SlackCardPoster` implements it with `chat.postMessage` and `chat.update`.

## Risks and open questions

- Repo visibility has to come from the agent's drafting run, since JotFlow holds no GitHub access. Until that is wired, the card says visibility was not checked, which is the safe default.
- Migration 5: I have asked the architect whether #106 or anything else in flight adds a migration. Whichever lands one first takes 5.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] `render_card` and `CardContext`
- [ ] Migration 5, `card_ts`, `set_card_ts`
- [ ] `CardPublisher`, `CardPoster`, `SlackCardPoster`
- [ ] Tests
- [ ] Docs: architecture Approval gate, AGENTS.md layout
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
