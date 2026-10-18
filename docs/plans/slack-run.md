# `jotflow run`: Socket Mode connection and redacted event logging

## Links and status

- Issue: #8
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: open the draft PR, then write the event filter in `src/jotflow/orchestrator/ingest.py`

This branch is stacked on #64 (Settings and `jotflow.toml`), which it needs. Once #64 merges, rebase onto `main`.

## Objective and done-when

`jotflow run` is the long-running entry point. It loads settings and policy, connects to Slack over Socket Mode, checks that the bot is in each configured channel, and logs incoming events as pointers only. It does no processing yet.

Done when:

- [ ] Connects with slack-bolt `AsyncApp` over Socket Mode, with the client's automatic reconnect on
- [ ] A message in a configured channel produces one log line with channel id and ts but no text
- [ ] A message in an unconfigured channel is ignored and logged at debug
- [ ] The bot's own messages, edits and deletes are ignored (D8)
- [ ] An event Slack delivers twice is logged once (D8)
- [ ] At startup, every configured channel the bot is not a member of is reported with the `/invite` instruction; the bot never joins on its own
- [ ] Missing environment variables and an invalid `jotflow.toml` stop the command with a clear message and exit code 2, before any network call
- [ ] Clean shutdown on SIGINT and SIGTERM
- [ ] Debug logging never prints message text, including from slack-bolt and slack-sdk
- [ ] `make check` passes

Stays open on #8 after this PR, because it needs the live sandbox workspace: staying connected through a real Socket Mode reconnect.

**Out of scope:** debouncing (#19), the general redacting logger (#25), the store (#11), channel modes, and anything that posts to Slack.

## Design

References: Components and Permissions in [architecture.md](../architecture.md); D8 in [roadmap.md](../roadmap.md); the invite-only decision recorded on #8 and in the manifest plan.

- **`orchestrator/ingest.py`**, pure and synchronous, so it is tested without Slack.
  - `BotIdentity(user_id, bot_id)` from `auth.test`.
  - `EventFilter(channels, identity)` turns a raw event into `Accepted` (a message or a reaction, as a `MessageRef`) or `Ignored` with a reason.
  - New messages are those with no subtype, or `thread_broadcast`, `file_share`, or `me_message`. Edits (`message_changed`), deletes (`message_deleted`), and every other subtype, such as joins and topic changes, are ignored. Editing a message never changes what is approved, so edits carry nothing JotFlow needs yet.
  - Own events: a message or reaction whose user is the bot user, or whose `bot_id` is the bot's, is ignored. Bolt drops self events by default too; JotFlow checks again so the rule is tested and does not depend on a library default.
  - Duplicates: a bounded memory of recent event ids and `(channel, ts)` keys for accepted events. In memory for now; restart recovery comes with run records in #11.
- **`orchestrator/slack.py`**: the Slack-facing side.
  - `fetch_identity(client)` calls `auth.test`.
  - `missing_channels(client, channel_ids)` calls `conversations.info` and returns the channels where `is_member` is false or the channel is not visible to the bot. It uses `channels:read` and `groups:read`, both in the manifest.
  - `register_handlers(app, filter)` attaches `message` and `reaction_added` listeners that log through the filter.
- **`commands/run.py`**: parses `--log-level`, configures logging, loads `Settings` and the policy, then runs `serve()` under `asyncio.run`. `serve()` connects the `AsyncSocketModeHandler`, waits on a stop event set by SIGINT or SIGTERM, and closes the handler.
- **Logging.** JotFlow's own lines format only ids and timestamps. The `slack_bolt` and `slack_sdk` loggers are held at INFO or above, because at DEBUG they print whole payloads, message text included.

Alternatives considered: `AsyncSocketModeHandler.start_async()` sleeps forever and gives no hook for a clean close, so `connect_async()` plus a stop event is used instead. Joining channels automatically would need `channels:join`, which the team lead ruled out.

## Risks and open questions

- Depends on #64. If its policy API changes in review, this branch follows it.
- Other bots' messages are treated like people's. Only JotFlow's own are dropped. A loop with another bot would need that bot to reply to JotFlow, which nothing JotFlow posts yet invites.
- A real reconnect cannot be exercised in CI. It stays a live criterion on #8.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Event filter with own-event, subtype, channel, and duplicate rules
- [ ] Slack identity, membership check, and handler registration
- [ ] `jotflow run`: settings and policy errors, logging, signals, clean close
- [ ] Add or update tests
- [ ] Update documentation
- [ ] `make check` passes
- [ ] Local CodeRabbit review
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
