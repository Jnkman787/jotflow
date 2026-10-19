# Help for people in the channel

## Links and status

- Issue: #80
- Also serves: #56 (`help_text()`), D10, D7
- Draft PR: #150
- Status: ready for review
- Last verified: 2026-10-19, `make check` passes with the code below
- Next safe action: review; the architect confirms the `help` policy key under Risks; renumber the migration if #145 lands first

## Objective and done-when

People forget the keywords. JotFlow helps them where they already are, quietly, and from one help text so no two surfaces disagree.

Done when:

- [x] One help source drives `/jotflow help`, the pinned guide, the newcomer tip, the near-miss nudge, and the README section, and a test fails if they drift
- [x] `/jotflow help` replies privately in any configured channel, with that channel's repos and approval mode. In an unconfigured channel it says the channel is not set up
- [x] The guide is posted and pinned once per channel, and never again, even after a restart
- [x] A person's first message in a channel gets a one-time private tip pointing at `/jotflow help`
- [x] A message that looks like a mistyped keyword gets a private suggestion with the right form, at most once per person per channel. JotFlow does not act on it
- [x] Near-miss detection has tests for real keywords, near misses, and ordinary sentences that must not trigger it, including "Skip button is broken"
- [x] Every notice is plain text, with no mentions or links that Slack would turn into pings. Tips and nudges are ephemeral, visible only to that person. Only the guide is a channel message
- [x] Each of the three is configurable per channel in `jotflow.toml`, with a top-level default
- [x] `make check` passes, including the coverage floor

**Out of scope:**
- A tip on a newcomer's first proposal card, which the issue also mentions. The lead asked for the tip on a newcomer's first message instead, and one tip is enough.
- The `member_joined_channel` event. Subscribing to it is a manifest change that teams would have to reinstall for. See the Design for how the guide is posted without it.

## Design

- **`orchestrator/help.py`** holds every word JotFlow says for help:
  - `HELP_TEXT`, today's `help_text()`, moved from `keyword_handler.py` and imported back so #135's callers keep working
  - `GUIDE`: a short version for the pinned message
  - `TIP`: one line pointing at `/jotflow help`
  - `nudge(keyword)`: "Did you mean `skip:`? Keywords need a colon. JotFlow did not act on this message."
  - All of them are built from `KEYWORDS` and `REACTIONS` in `keywords.py`, so adding a keyword updates every surface.
- **Near misses**, as `near_miss(text) -> Keyword | None` in `keywords.py`, next to `split_keyword`, so there is still one keyword module. A near miss is any of these:
  - a whole message that is just a keyword, such as `skip`
  - a keyword followed by `-`, `–`, `—`, `;`, `,`, `.`, or `=`, such as `Idea - dark mode` or `skip; testing`
  - `re` followed by an issue reference, such as `re 42` or `re web#42`
  - Not: a keyword followed by an ordinary word, such as "Skip button is broken" or "Bug reports are slow", and not anything `split_keyword` already accepts
- **`HelpHandler(store, policy, slack)`**, called from `Orchestrator.note` after the keyword handler, for accepted human messages:
  - It recognises a keyword: the newcomer tip is claimed silently, since that person already knows the keywords.
  - It sees a near miss: the nudge is claimed and sent as an ephemeral reply in the message's thread, and the tip is claimed too.
  - It sees anything else from someone new to the channel: the tip is claimed and sent the same way.
  - At most one notice per message. The tip and the nudge are each once per person, so someone who got the tip can still get one nudge later.
  - Nothing is sent in a paused channel, by the orchestrator's check of the channel's mode.
  - `ensure_guide(channel)` posts the guide and pins it, then records it. It runs at startup for each configured channel the bot is in, and on the first accepted event from a channel without one. So a channel the bot is invited to later gets its guide with its first message, without the manifest change.
- **Once only, across restarts**: a new table, `help_notices (channel_id, user_id, kind, sent_at)`, with a primary key across the first three. The guide uses `user_id = ''`. `Store.claim_notice(channel_id, user_id, kind) -> bool` is an insert-or-ignore, true only for the first claim. A notice is claimed before it is sent, so two quick messages never both get one. If the send fails, the notice is not retried, which errs on the side of quiet. Memory and SQLite stores both get it, plus a contract test.
- **Posting**: blocks with one `plain_text` section, plus a fallback `text`, and `unfurl_links` off. Ephemerals use `chat.postEphemeral` with `thread_ts` for a reply. The guide uses `chat.postMessage` and `pins.add`. All of them fit the existing scopes: `chat:write` and `pins:write`.
- **`/jotflow help`** reads the command's `channel_id`. In a configured channel it shows `HELP_TEXT`, the channel's repo nicknames, and its approval mode, from the store's channel mode or the policy default. Elsewhere it says "This channel isn't set up for JotFlow yet. An admin adds it to jotflow.toml."
- **README**: the "Using it" keyword bullets are replaced by a block between `<!-- jotflow-help -->` markers, and a test checks it matches `HELP_TEXT` line for line.
- **Policy**: see Risks for the key.

## Risks and open questions

- **The policy key** (asked of the architect). Default: a top-level `[help]` table with `guide`, `newcomer_tip`, and `near_miss_nudge`, each true by default. A channel can override any of them in `[channels.<id>.help]`, the same way `debounce_seconds` overrides per channel.
- **Migration numbers.** #145 adds migration 6. This PR's table takes the next free number when it is rebased.
- **Noise on an existing channel.** When JotFlow first runs in a busy channel, everyone's first message gets the tip once. It is private and one line. A team that finds it noisy turns `newcomer_tip` off.
- A guide someone unpins is not posted again, which respects that choice.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; the policy key pending the architect
- [x] Commit this plan and open the draft PR
- [x] `help.py`: the texts, from the keyword module
- [x] `near_miss` in `keywords.py`, with tests
- [x] Store: `help_notices` and `claim_notice`, both stores, contract test, schema test
- [x] Policy: the `help` table and per-channel override
- [x] `HelpHandler`: guide, tip, nudge, wired after the keyword handler
- [x] `/jotflow help` per channel
- [x] README block and the drift test
- [x] CodeRabbit once: skipped, the free CLI allowance was rate-limited
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` passes. `help.py`, `keywords.py`, and both stores are at 100% coverage.
- `tests/test_help.py` covers several things:
  - every keyword and reaction appears in the help text and the guide, and the README block matches the help text exactly
  - no help text holds `<` or `@`
  - `/jotflow help` per channel, mode, approvers, and an unconfigured channel
  - the guide pinned once across two handlers, as after a restart
  - the tip once, no tip for someone who uses a keyword, and one nudge with no tip after it
  - the per-channel and top-level switches, and the Bolt adapter's plain-text, no-unfurl calls
- `tests/test_keyword_handler.py` has near misses, and ordinary sentences that must not trigger one, including "Skip button is broken", "Ship - v2", and "re-run the export".
- `tests/test_service.py` sends a first message and a near-miss reply through a real Bolt app. It shows the guide pinned and the tip and then the nudge whispered, nothing in a paused channel, guides pinned once at startup, and a failed send that is logged without stopping the debouncer.
- The store contract tests cover claims once per person, kind, and channel, bad claims, and a SQLite claim surviving a restart.

## Handoff notes

None yet.
