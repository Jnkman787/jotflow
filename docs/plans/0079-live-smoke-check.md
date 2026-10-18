# Opt-in live smoke check

## Links and status

- Issue: #79
- Draft PR: #83
- Status: completed
- Last verified: `make check` green locally on Python 3.12 after rebasing onto `main`: 285 tests, 96.55% coverage
- Next safe action: review and merge #83; then the maintainer runs `JOTFLOW_LIVE_CHECK=1 make live-check` in the sandbox workspace with JotFlow stopped, and ticks the live items on #79 and the issues it covers

## Objective and done-when

A maintainer runs one command against a real Slack workspace to prove that a fresh install works end to end. That covers the remaining live done-when items of #5, #7, #8, #9, and #10. The command is generic: it reads the same `.env` and `jotflow.toml` as any install, and nothing in the repository names a workspace or channel. It never runs in CI.

Done when:

- [x] `jotflow check --live` runs against any workspace configured with `jotflow init`, with no hard-coded workspace details
- [x] It refuses to run unless `JOTFLOW_LIVE_CHECK=1` is set, refuses outright when `CI` is set, and names the channel it will post to before posting
- [x] It deletes its own test message, including after a failure partway through
- [x] Unit tests cover its logic with fakes; no CI job runs it live
- [x] README troubleshooting names it as the way to verify a fresh install

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
5. **Receive.** Wait for the event whose channel and ts match the posted message. Slack sends each event to only one Socket Mode connection, so a running JotFlow could take it. The command says to stop JotFlow before posting, and the timeout message repeats it, along with a hint about the `message.channels` and `message.groups` event subscriptions.
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
- [x] Commit this plan and open the draft PR
- [x] Core steps and the `LiveSlack` protocol in `live_check.py`
- [x] `BoltLiveSlack` adapter
- [x] `jotflow check` with `--live`, opt-in, CI refusal, and channel choice; `make live-check`
- [x] Add or update tests
- [x] Update documentation: README troubleshooting, AGENTS.md layout and commands
- [x] `make check` passes
- [x] Local CodeRabbit review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make check`: line endings, ruff, and mypy strict are clean. 285 tests pass, with total coverage at 96.55%. `live_check.py` and `commands/check.py` are both at 100%. 42 tests are new, in `tests/test_live_check.py` and `tests/test_check_command.py`.
- The steps are tested with a fake `LiveSlack`:
  - a full pass in the expected call order
  - the message text never reaching the report
  - unrelated events skipped
  - the legacy `bot_message` subtype
  - a bad token, a non-member, a Socket Mode failure, or a failed post: nothing is posted or deleted where it should not be
  - a receive timeout that still deletes the message
  - an own message that is accepted
  - text in the log, and a log line without pointers
  - a message ignored for others
  - a failed delete, which names the ts
  - the ingest logger restored afterwards
- The adapter is tested against fake Bolt objects: scopes and ids, missing scopes, an unreachable Slack, membership and missing scope, message routing and an idempotent close, connect failures, post and delete, their failures, and a missing ts. A real `AsyncApp` built by the adapter has no `AsyncIgnoringSelfEvents` middleware; a default `AsyncApp` has it, so that test is not vacuous.
- The command is tested for:
  - offline success and configuration errors
  - refusing without the opt-in
  - refusing with the opt-in only in `.env`
  - refusing when `CI` is `true`, `1`, or `yes`; `CI=false` does not block
  - naming the channel and the stop-JotFlow note before posting
  - a non-positive timeout
  - the channel choice rules
  - building the real adapter
- Manual, offline, with made-up tokens and the example policy:
  - `jotflow check` exits 0
  - `--live` without the opt-in exits 2
  - with `CI=true` it exits 2
  - with two configured channels and no choice it exits 2 and lists them
- No workspace, channel, or company name appears in the diff.
- CodeRabbit (local CLI, `--committed --base main -c AGENTS.md`): 2 findings, both fixed. One: Slack sends each event to only one Socket Mode connection, so the command, the timeout message, and the README now say to stop JotFlow first. Two: this plan's status and validation were not yet filled in.
- Security review: approved, with 3 nits, all taken.
  1. The claim that the opt-in "cannot be left on" overstated it. The check now refuses when `.env` contains `JOTFLOW_LIVE_CHECK`, and the docstring and README say what remains. Through Compose, `env_file` turns `.env` lines into the container's environment, and a shell profile can export the opt-in. A check inside the container cannot see the host's `.env`, so for that path the guards are the README rule and the prompt.
  2. The README now says the reverse risk too: while the check is connected, a running JotFlow can miss real messages.
  3. In a terminal the check asks "Post one test message to channel C...? [y/N]" before posting. `--yes` skips the question, and scripts without a terminal are not asked. A "no" or a closed input posts nothing and exits 2.
- After the review: 290 tests pass, with total coverage at 96.59% and `commands/check.py` at 100%.
- Not done here: the live run against the sandbox workspace. That is the maintainer's, and it is why the PR says `Refs #79`.

## Follow-up after #83

Team lead decision, recorded on #79: the live check refuses inside JotFlow's own image. The follow-up PR is `fix(cli): refuse the live check inside the JotFlow image`, Refs #79.

- **Image refusal.** The Dockerfile's runtime stage sets `JOTFLOW_IMAGE=1`, and `check --live` refuses when it is set: "run the live check on the host: JOTFLOW_LIVE_CHECK=1 make live-check". This closes the Compose path, where `env_file` would carry an opt-in left in `.env` into the container's environment. Plain `jotflow check` still works in the image. A devcontainer does not set the marker, so it is not blocked.
- **Clearer message.** An opt-in left in `.env` now gets "remove the JOTFLOW_LIVE_CHECK line from .env and set it only for this one run".
- **Ctrl-C.** Ctrl-C at the confirmation prompt prints "cancelled, nothing posted" and exits 130, instead of a traceback. This was security's note.
- **Tests.** Unit tests cover all three. The image job in CI builds the image and checks two things: `jotflow check` passes inside it, and `jotflow check --live` refuses with the host instruction. Both use dummy tokens.

## Handoff notes

- Run it from the repository folder, with JotFlow stopped, using `docker compose stop jotflow`:
  ```sh
  JOTFLOW_LIVE_CHECK=1 make live-check
  ```
  Add `--channel` through `uv run jotflow check --live --channel C...` if `jotflow.toml` lists several channels. Start JotFlow again afterwards.
- A pass covers these live items:
  - #5: the app installs from the manifest; the bot reads a channel it is in, and private channels when a private one is chosen
  - #7 and #10: the tokens `jotflow init` saved work
  - #8: connects, and its event filtering and redaction hold on a real payload
- The reconnect criterion on #8 still needs a manual network drop. The check does not simulate one.
