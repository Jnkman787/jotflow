# Help for people in the channel

## Links and status

- Issue: #80
- Also serves: #56 (`help_text()`), D10, D7
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-19, no code yet
- Next safe action: the architect confirms the `help` policy key under Risks; then add `orchestrator/help.py`

## Objective and done-when

People forget the keywords. JotFlow helps them where they already are, quietly, and from one help text so no two surfaces disagree.

Done when:

- [ ] One help source drives `/jotflow help`, the pinned guide, the newcomer tip, the near-miss nudge, and the README section, and a test fails if they drift
- [ ] `/jotflow help` replies privately in any configured channel, with that channel's repos and approval mode. In an unconfigured channel it says the channel is not set up
- [ ] The guide is posted and pinned once per channel, and never again, even after a restart
- [ ] A person's first message in a channel gets a one-time private tip pointing at `/jotflow help`
- [ ] A message that looks like a mistyped keyword gets a private suggestion with the right form, at most once per person per channel. JotFlow does not act on it
- [ ] Near-miss detection has tests for real keywords, near misses, and ordinary sentences that must not trigger it, including "Skip button is broken"
- [ ] Every notice is plain text, with no mentions or links that Slack would turn into pings. Tips and nudges are ephemeral, visible only to that person. Only the guide is a channel message
- [ ] Each of the three is configurable per channel in `jotflow.toml`, with a top-level default
- [ ] `make check` passes, including the coverage floor

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
  - It sees a near miss: the nudge is claimed and sent as an ephemeral reply in the message's thread.
  - It sees anything else from someone new to the channel: the tip is claimed and sent the same way.
  - At most one notice per message.
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
- [ ] Commit this plan and open the draft PR
- [ ] `help.py`: the texts, from the keyword module
- [ ] `near_miss` in `keywords.py`, with tests
- [ ] Store: `help_notices` and `claim_notice`, both stores, contract test, schema test
- [ ] Policy: the `help` table and per-channel override
- [ ] `HelpHandler`: guide, tip, nudge, wired after the keyword handler
- [ ] `/jotflow help` per channel
- [ ] README block and the drift test
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
