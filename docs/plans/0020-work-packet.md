# Work packet: live thread fetch, mentions, and related excerpts

## Links and status

- Issue: #20
- Also serves: #56, for the strict keyword prefix only; #14's `WorkSource` interface
- Draft PR: #125
- Status: completed
- Last verified: `make check` green locally: 1273 tests, `packet.py` at 99%, `keywords.py` at 100%
- Next safe action: review and merge #125. #55 wires `SlackWorkSource(BoltSlackReader(app.client), store, bot=identity)` into `read_tools`

## Objective and done-when

When a drafting or question run calls `get_work`, JotFlow fetches the run's thread from Slack by its timestamp. It resolves who wrote each message and who is mentioned, and builds the packet #14 serves. The text exists only in that packet, in memory, for that call. It is never stored or logged.

Done when:

- [x] The packet contains only the thread in question, fetched with `conversations.replies`. No channel history is ever read.
- [x] Text is not persisted anywhere: not in the store, not in logs, not in a cache
- [x] Slack rate-limit responses are retried with backoff that honours `Retry-After`, then fail clearly
- [x] `skip:` messages are excluded, and a `skip:` root excludes the whole thread. Messages a person marked as chatter are excluded too.
- [x] Keyword prefixes (`idea:`, `bug:`, `re:`) are stripped from the text the agent sees, and passed as a separate hint field
- [x] `<@U…>` mentions read as display names, with the Slack user id kept alongside. Authors carry a display name and user id too.
- [x] Very long threads are trimmed: the root, the first replies, and the most recent replies are kept, with a note saying how many were left out. Each message and the whole packet have a size cap.
- [x] Related-thread excerpts: short excerpts from the channel's other active topic threads, ranked by word overlap with this thread, capped
- [x] Several topics in one thread: each message says which topic it is already linked to, if any
- [x] `make check` passes

**Out of scope:**

- the full keyword feature: reactions, typo tolerance, `re:` resolution, acknowledgement reactions, and `/jotflow help`, all of which are #56
- wiring `jotflow run`, which is #55
- semantic search for related threads; this is lexical overlap only

## Design

References: D9 and D10 in [roadmap.md](../roadmap.md); the Data and privacy and Conversation rules sections in [architecture.md](../architecture.md); #14's `WorkSource` protocol.

**`orchestrator/keywords.py`: the strict prefix only.** `split_keyword(text) -> (keyword | None, rest)` recognises exactly `idea:`, `bug:`, `re:`, and `skip:` as the first word, case-insensitive. The colon is required, so "Skip button is broken" is ordinary text. #56 extends this module with typo tolerance and reactions. Owning the prefix rule in one module means #20 and #56 can never disagree about what counts as a keyword.

**`orchestrator/packet.py`: `SlackWorkSource`,** the real `WorkSource` behind a small `SlackReader` protocol, so tests use a fake.

- `thread(channel, thread_ts)` pages through `conversations.replies` for that ts.
  - It drops `skip:` messages, and the whole thread if the root says `skip:`.
  - It drops messages the store says a person marked as chatter, which is how a `skip` reaction will arrive from #56 or #33.
  - It drops edits and deletes, using the same subtype rules as `ingest.EventFilter`.
  - It strips keyword prefixes into `ThreadMessage.hint`.
  - It resolves authors and `<@U…>` mentions through `users.info`. Channel references, links, and `<!here>` become plain readable forms.
  - It attaches each message's existing topic link from the store.
  - It trims the thread to the caps.
- `related(channel, thread_ts, topics)` takes the other threads listed in the topic index, and ranks them by word overlap with this thread. It fetches the root of the top few, at one `conversations.replies` call each with limit 1, and excerpts each to a short length.
- **Rate limits.** Every Slack call goes through one helper. On a 429, it waits `Retry-After`, or exponential backoff if the header is missing, with a cap, up to a few attempts. Then it raises a `ToolError` saying Slack is rate limiting. The sleep function is injected, so tests take no time.
- **Nothing persists.** The source holds no text between calls. A display-name cache maps only user ids to names, which are directory data rather than message content, bounded and short-lived. A test opens a `SqliteStore` file after `get_work`, and checks the thread's text is absent. Log lines name ids and counts only.

**Model changes in `whiteboard/read_tools.py`,** all additive:

- `ThreadMessage` gains `hint`, the keyword, `mentions`, a list of `{user_id, display_name}` with the name marked untrusted, and `topic_id`.
- `WorkThread` gains `omitted`, the count of messages trimmed.

**Caps** are constants, tuned later:

| Cap | Value |
|---|---|
| Messages per thread | 200 |
| Characters per message | 4,000 |
| Characters per packet | 60,000 |
| Kept when trimming | the root, the first 10, and the last 60 |
| Related excerpts | 3, of 280 characters each |

Alternatives considered:

- slack-sdk's `AsyncRateLimitErrorRetryHandler`. It retries, but it cannot be tested without HTTP mocking, and it gives no clear failure at the end. One small helper is simpler to test and to reason about.
- Fetching the whole channel's history to find related threads. Ruled out by #20's first box, and by D9.

## Risks and open questions

- The overlap with #56 is limited to `keywords.py`'s prefix rule, and I have checked it with the team lead. #56 builds on that module rather than replacing it.
- Lexical overlap is a weak relatedness signal. It is enough for v1, because the agent also gets the topic index and can call `get_topic`.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `keywords.py` strict prefix
- [x] Slack call helper with rate-limit backoff
- [x] `SlackWorkSource.thread`: fetch, filter, strip, resolve, trim
- [x] `SlackWorkSource.related`: rank, fetch roots, excerpt
- [x] Model additions in `read_tools.py`
- [x] Add or update tests, including the no-persistence test
- [x] Update documentation: the architecture Data and privacy section and the work packet
- [x] `make check` passes
- [x] Local CodeRabbit review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make check` passes, with 1273 tests. `orchestrator/packet.py` is at 99%, and `orchestrator/keywords.py` at 100%.
- `tests/test_packet.py`, 40 tests, all against a fake `SlackReader` that logs every call, so no network. They cover:
  - **Keywords:** the strict prefix, including "Skip button is broken" and "ideas:" as plain text.
  - **Markup:** Slack markup to plain text, covering mentions, channels, links, `<!here>`, user groups, and entities.
  - **Only the thread is read:** the log shows only `replies` for the run's ts, plus `user` lookups.
  - **Filtering:**
    - keyword hints are stripped from the text
    - `skip:` messages are dropped, with their text absent from the packet
    - a `skip:` root refuses the whole thread
    - a person's chatter mark drops a message, while the agent's does not
    - per-message topic links are shown, for several topics in one thread
    - JotFlow's own messages are marked, other bots are kept, and joins are dropped
  - **Names:**
    - names are resolved, with ids kept, and fall back to the id
    - each person is looked up once, and the cache expires and is bounded, holding names only
  - **Long threads:** paging, trimming to root, head, and tail with the omitted count, and the per-message and packet caps.
  - **Rate limits:** `Retry-After` honoured, doubling without it, a cap on long waits, a bad header, a clear failure after the attempts run out, and other errors failing at once.
  - **Related excerpts:**
    - ranked by shared title words, excluding the run's own thread and anything with no overlap
    - capped and cut, with `skip:` roots, person-chatter roots, and failing threads left out
    - mentions resolved in excerpts
    - an unreadable thread giving no excerpts
  - **The real adapter:** `BoltSlackReader` asks for exactly one thread and one user.
- **No persistence:** a `SqliteStore` file runs a full `get_work` through `read_tools` with this source. The secret text is in the packet served to the agent, and absent from every database file afterwards.
- CodeRabbit: see the PR.

## Handoff notes

- **For #55.** Build the source as `SlackWorkSource(BoltSlackReader(app.client), store, bot=identity)` and pass it to `read_tools`. `bot` is the `BotIdentity` from `fetch_identity`, so JotFlow's own questions are marked `from_jotflow`.
- **For #56.** Extend `orchestrator/keywords.py`, and keep `split_keyword` as the single prefix rule. A `skip` reaction should call `store.mark_chatter(ref, by="person")`; the packet already excludes those messages.
- **Tuning.** The caps are module constants in `packet.py`. Related-thread ranking is word overlap with topic titles, which is deliberately simple for v1.
