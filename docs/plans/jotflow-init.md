# `jotflow init` guided setup

## Links and status

- Issue: #7
- Draft PR: #67
- Status: implementing
- Last verified: `make check` green locally on 2026-10-18, 97 tests
- Next safe action: wait for #64 to merge, rebase, then add the channel, repo, and preset questions and write `jotflow.toml` with `jotflow.policy`

## Objective and done-when

`jotflow init` walks a team from a fresh clone to a connected bot. It asks a few questions, renders the Slack manifest with the team's bot name, prints the create-from-manifest link, waits for the two tokens, validates them against Slack with messages that name the problem, and writes `.env` and `jotflow.toml`. Slack's two manual steps, creating the app and clicking Allow, are wrapped, not removed (D2).

Done when:

- [x] Asks for the bot name and renders the manifest with `render_manifest_json`
- [x] Prints the create-from-manifest link and the path of a manifest file as a fallback
- [x] Reads the bot token and app-level token without echoing them
- [x] Validates the bot token with `auth.test` and the app token with `apps.connections.open`
- [x] A token with a missing scope produces a message naming each missing scope
- [x] A token of the wrong kind or a revoked token produces a specific message, and the user can paste again
- [x] Writes `.env` with the two tokens, readable only by its owner, keeping unrelated lines
- [ ] Asks for channel ids, each channel's repos with nickname and description (D11), and the agent preset, then writes `jotflow.toml`
- [ ] Re-running is idempotent and offers to keep existing values (done for the tokens; `jotflow.toml` waits for #64)
- [x] Prompts and Slack calls are injected, so tests use fakes and need no network
- [ ] `make check` passes

Stays open on #7 after this PR, because it needs the live sandbox workspace:

- Runs end to end on a fresh clone in under five minutes of user time

**Out of scope:** the agent smoke test (M3 presets). Creating the app through Slack's manifest API (post-v1). Joining channels: the bot is invited by a team member, and `jotflow run` (#8) reports channels it is not in.

## Design

Order of work. The `jotflow.toml` part waits for #64, which defines the policy schema in `jotflow.policy`. Everything else does not depend on it and lands first.

- `src/jotflow/slack_auth.py`: token checks.
  - `SlackApi` protocol with `call(method, token) -> ApiResult`, where `ApiResult` holds the JSON body and the scopes from Slack's `X-OAuth-Scopes` header.
  - `HttpSlackApi` is the real client, using the standard library's HTTPS support so no dependency is added. The Slack SDK is only a transitive dependency of slack-bolt, so this code does not import it.
  - `check_bot_token(api, token)` checks the `xoxb-` prefix, calls `auth.test`, and compares granted scopes with `slack_manifest.BOT_SCOPES`. It returns the workspace and bot user, or raises `TokenError` with a message for the person.
  - `check_app_token(api, token)` checks the `xapp-` prefix and calls `apps.connections.open`. A `missing_scope` error names `connections:write`.
- `src/jotflow/envfile.py`: reads and updates `KEY=value` files. It keeps comments and unrelated keys, writes atomically, and sets mode 0600. Keys are `JOTFLOW_SLACK_BOT_TOKEN` and `JOTFLOW_SLACK_APP_TOKEN`, matching `Settings`.
- `src/jotflow/commands/init.py`: the interview. A `Console` takes injected input, secret input, and output, so tests script the whole session.
  - Re-run: if `.env` holds tokens that still validate, init reports the connected workspace and bot and offers to keep them, skipping the manifest step.
  - Flags: `--env-file` and `--config` for the two output paths, and `--manifest-out` for where to save the rendered manifest.
- Without a terminal on stdin, init exits with code 2 and a message instead of waiting for input. The rendered manifest goes to `slack-app-manifest.json`, which is gitignored.
- Create-from-manifest link: `https://api.slack.com/apps?new_app=1&manifest_json=<url-encoded JSON>`. The rendered manifest is also saved to a file in case the link is too long for a terminal or browser.

Security:

- Tokens are read with hidden input, never printed, never logged, and only written to `.env`. Error messages never include the token.
- `.env` is created with mode 0600 before any secret is written.

## Risks and open questions

- Waiting on #64 for the policy schema. Until then the `jotflow.toml` steps stay unchecked.
- The create-from-manifest link format is not formally documented by Slack. The saved manifest file and the steps in `manifests/README.md` are the fallback.
- `apps.connections.open` asks Slack for a Socket Mode URL but does not connect. Opening the connection is `jotflow run` (#8).

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Token checks in `slack_auth.py` with a fake-friendly interface
- [x] `.env` reader and writer
- [x] Interview: bot name, manifest, link, tokens, `.env`, re-run behaviour
- [ ] After #64 merges: channels, repos, agent preset, and `jotflow.toml`
- [ ] Add tests
- [ ] Update documentation: README quickstart pointer, `manifests/README.md`
- [ ] CodeRabbit review, once
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- 2026-10-18, before #64: `make check` green, 97 tests. New: `tests/test_slack_auth.py`, `tests/test_envfile.py`, `tests/test_init.py`, with shared fakes in `tests/fakes.py`. Init sessions are scripted end to end with a fake Slack client; no network.

## Handoff notes

None yet.
