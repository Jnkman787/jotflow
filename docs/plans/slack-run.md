# `jotflow run`: Socket Mode connection and redacted event logging

## Links and status

- Issue: #8
- Draft PR: #66
- Status: blocked: waiting for #64 to merge, then rebase onto `main`
- Last verified: `make check` green locally on Python 3.12, 100 tests
- Next safe action: after #64 merges, `git rebase --onto origin/main feat/config-schema feat/slack-run`, retarget #66 to `main`, rerun `make check` and the local CodeRabbit review, then mark #66 ready

This branch is stacked on #64 (Settings and `jotflow.toml`), which it needs. Once #64 merges, rebase onto `main`.

## Objective and done-when

`jotflow run` is the long-running entry point. It loads settings and policy, connects to Slack over Socket Mode, checks that the bot is in each configured channel, and logs incoming events as pointers only. It does no processing yet.

Done when:

- [x] Connects with slack-bolt `AsyncApp` over Socket Mode, with the client's automatic reconnect on
- [x] A message in a configured channel produces one log line with channel id and ts but no text
- [x] A message in an unconfigured channel is ignored and logged at debug
- [x] The bot's own messages, edits and deletes are ignored (D8)
- [x] An event Slack delivers twice is logged once (D8)
- [x] At startup, every configured channel the bot is not a member of is reported with the `/invite` instruction; the bot never joins on its own
- [x] Missing environment variables and an invalid `jotflow.toml` stop the command with a clear message and exit code 2, before any network call
- [x] Clean shutdown on SIGINT and SIGTERM
- [x] Debug logging never prints message text, including from slack-bolt and slack-sdk
- [x] `make check` passes

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
- [x] Commit this plan and open the draft PR
- [x] Event filter with own-event, subtype, channel, and duplicate rules
- [x] Slack identity, membership check, and handler registration
- [x] `jotflow run`: settings and policy errors, logging, signals, clean close
- [x] Add or update tests
- [x] Update documentation
- [x] `make check` passes
- [ ] Local CodeRabbit review
- [ ] Mark the PR ready for review

## Validation

- `make check`: ruff lint and format clean, mypy strict clean, 100 tests pass. 47 are new, in `tests/test_ingest.py` and `tests/test_run.py`.
- The tests cover:
  - accepted messages and their subtypes
  - unconfigured channels
  - own messages, by user id and by bot id
  - other bots
  - edits, deletes, and other subtypes
  - malformed events
  - duplicates, by event id and by channel and ts, and the bounded memory
  - reactions: own, unconfigured, on files, duplicates
  - one INFO line per accepted message without its text
  - ignored events at DEBUG
  - missing tokens, a bad or missing policy file exiting 2 without calling Slack
  - the Slack libraries held at INFO under `--log-level debug`
  - connect then close, close on cancel
  - SIGINT and SIGTERM setting the stop event
  - a bad token, an unreachable Slack, a missing scope
  - non-member and invisible channels with the `/invite` hint
  - the registered listener logging a pointer once for a duplicated delivery
- Manual: `jotflow run` with no tokens and with no `jotflow.toml` prints the problems and exits 2.
- CodeRabbit: skipped. The free CLI allowance was used up, with a reset in about 42 minutes. In its place a self-review found two gaps, both fixed: a network failure at startup ended in a traceback instead of a message, and a failed Socket Mode connect left the handler open.
- Not yet done: staying connected through a real Socket Mode reconnect, which needs the sandbox workspace. The handler uses slack-sdk's automatic reconnect, which is on by default.

## Handoff notes

- `tests/test_cli.py` now runs each subcommand from an empty directory with `JOTFLOW_*` cleared. Without that, `run` on a developer machine with a real `.env` and `jotflow.toml` would connect to Slack during the test.
- Duplicate memory is in-process. A restart forgets it, and Slack does not redeliver old events after a reconnect, so this is acceptable until #11.
- The debouncer (#19) should hook in where `register_handlers` calls `log_decision`.
