# Opt-in live smoke check

## Links and status

- Issue: #79
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: open the draft PR, then write the check's core in `src/jotflow/live_check.py`

## Objective and done-when

A maintainer runs one command against a real Slack workspace to prove that a fresh install works end to end. That covers the remaining live done-when items of #5, #7, #8, #9, and #10. The command is generic: it reads the same `.env` and `jotflow.toml` as any install, and nothing in the repository names a workspace or channel. It never runs in CI.

Done when:

- [ ] `jotflow check --live` runs against any workspace configured with `jotflow init`, with no hard-coded workspace details
- [ ] It refuses to run unless `JOTFLOW_LIVE_CHECK=1` is set, refuses outright when `CI` is set, and names the channel it will post to before posting
- [ ] It deletes its own test message, including after a failure partway through
- [ ] Unit tests cover its logic with fakes; no CI job runs it live
- [ ] README troubleshooting names it as the way to verify a fresh install

This PR uses `Refs #79`. The live run itself is the maintainer's, and #79 stays open until it passes.

**Out of scope:** running JotFlow's full event loop, checking the container image (CI already smoke-tests it), and posting anywhere but one configured channel.

## Design

References: Permissions and Components in [architecture.md](../architecture.md); D4 (replay plus a live sandbox) in [roadmap.md](../roadmap.md).

**Command.** A new subcommand, `jotflow check`, in `src/jotflow/commands/check.py`, plus `make live-check`.

- With no flags it checks configuration offline: it loads settings and `jotflow.toml` exactly as `jotflow run` does, then reports success or the problems. It makes no network call. A `check` command that only works with `--live` would be a surprising default, and this costs a few lines.
- `--live` adds the Slack round trip. Options:
  - `--channel C...` picks the channel. It falls back to `JOTFLOW_LIVE_CHECK_CHANNEL`, then to the only configured channel. The channel must be one listed in `jotflow.toml`, so the check can never post somewhere JotFlow is not meant to be.
  - `--timeout` defaults to 30 seconds.
- Refusals exit 2 before any network call:
  - `JOTFLOW_LIVE_CHECK` is not `1`
  - `CI` is set; this is belt and braces, since CI holds no Slack tokens
  - the channel cannot be resolved

**Steps** (`src/jotflow/live_check.py`). Each prints `ok` or `FAIL` with a specific reason, and the first failure exits 1.

1. **Bot token.** `slack_auth.check_bot_token` confirms the token and every manifest scope, so the check reuses `jotflow init`'s messages. `orchestrator.slack.fetch_identity` gives the bot user id, bot id, and name.
2. **Membership.** `orchestrator.slack.missing_channels` for the chosen channel. A non-member fails with the `/invite` instruction.
3. **Socket Mode.** Connect with the app token and listen for `message` events. Bolt's self-event filter is turned off for this connection only, because the point is to receive the bot's own message.
4. **Post.** Print the channel id, then post one message marked as a JotFlow live check, with a random nonce. Everything after this step runs inside `try`/`finally` so the message is always deleted.
5. **Receive.** Wait for the event whose channel and ts match the posted message. A timeout fails with a hint about the `message.channels` and `message.groups` event subscriptions.
6. **Self-filter.** Run the received payload through JotFlow's real `EventFilter` with the bot's identity. It must be ignored. That is D8's rule, checked against a real Slack payload instead of a fixture.
7. **Redaction.** Run the same payload through a filter that treats the sender as a stranger. Capture the log line `log_decision` writes, and check that it carries the channel id and ts but not the message text.
8. **Clean up.** `chat.delete` the test message, then close the connection. If the delete fails, the check prints the ts so the message can be removed by hand, and exits 1.

**Testability.** The steps talk to Slack only through a small `LiveSlack` protocol: identity, membership, connect, post, delete, close. The tests drive every step and failure with a fake. The real implementation, `BoltLiveSlack`, is a thin adapter over Bolt's `AsyncApp`, the Socket Mode handler, and the existing helpers. Its methods are tested against fake clients the same way `jotflow run`'s are. Nothing opens a socket.

**Exclusion from CI.** The live path needs real tokens and `JOTFLOW_LIVE_CHECK=1`, CI has neither, and the command refuses when `CI` is set. No workflow calls `make live-check`.

## Risks and open questions

- Slack may deliver the bot's own message with the legacy `bot_message` subtype. Either way the self-filter step passes, because the message must be ignored; the reason is printed. For the redaction step, the stranger copy drops `bot_id` and that subtype, so the line for an accepted message is what gets checked.
- The test message is briefly visible in the sandbox channel. It is clearly marked and deleted within seconds.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Core steps and the `LiveSlack` protocol in `live_check.py`
- [ ] `BoltLiveSlack` adapter
- [ ] `jotflow check` with `--live`, opt-in, CI refusal, and channel choice; `make live-check`
- [ ] Add or update tests
- [ ] Update documentation: README troubleshooting, AGENTS.md layout and commands
- [ ] `make check` passes
- [ ] Local CodeRabbit review, or skipped
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
