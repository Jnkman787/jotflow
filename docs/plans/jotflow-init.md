# `jotflow init` guided setup

## Links and status

- Issue: #7
- Draft PR: #67
- Status: completed
- Last verified: `make check` green locally on 2026-10-18 after the security review, 237 tests
- Next safe action: in the sandbox workspace, run `jotflow init` on a fresh clone and time it, for the live criterion on #7

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
- [x] Asks for channel ids, each channel's repos with nickname and description (D11), and the agent preset, then writes `jotflow.toml`
- [x] Re-running is idempotent and offers to keep existing values: working tokens, the whole `jotflow.toml`, or each channel's repos
- [x] Prompts and Slack calls are injected, so tests use fakes and need no network
- [x] `make check` passes

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
- `jotflow.toml` (after #64): the interview builds a `Policy` and `policy_writer.render_policy` turns it into TOML, then parses its own output with `parse_policy` and refuses to return text that does not load back to the same policy. The standard library cannot write TOML and no dependency is added, so the writer covers the shapes the schema uses, with careful string escaping.
  - Questions: channel id (a pasted channel link works), then for each repo `owner/name` (a GitHub link works), nickname (suggested from the repo name), a one-line description (D11), and labels (suggested `bug, enhancement`). Then the agent preset, or `none`.
  - Each answer is checked by the policy's own field types, so prompts show the same messages as the loader. Duplicate channels, repos, and nicknames are asked again.
  - Re-run: an existing valid file can be kept whole. If it is rewritten, settings init does not ask about (issue links, debounce, approval) are kept, a channel already listed can keep its repos, and the old file is saved as `jotflow.toml.bak`. An invalid file shows the loader's problems and can be kept or replaced.
  - Both files are written only after the last question, so stopping part-way changes nothing.
- Docker (after #68, team lead decisions): `jotflow.toml` is written with mode 0644, because the container's uid 10001 reads it from a read-only mount and it holds no secrets. `.env` stays 0600. Keeping an existing policy file that others cannot read prints the `chmod 644` fix. Init also adds `COMPOSE_FILE=docker-compose.yml:docker-compose.policy.yml` to `.env` when the key is absent, so plain `docker compose up -d` mounts the policy. A different value already there is kept, with a note.
- Create-from-manifest link: `https://api.slack.com/apps?new_app=1&manifest_json=<url-encoded JSON>`. The rendered manifest is also saved to a file in case the link is too long for a terminal or browser.

Security:

- Tokens are read with hidden input, never printed, never logged, and only written to `.env`. Error messages never include the token.
- `.env` is created with mode 0600 before any secret is written.

## Risks and open questions

- The create-from-manifest link format is not formally documented by Slack. The saved manifest file and the steps in `manifests/README.md` are the fallback.
- `apps.connections.open` asks Slack for a Socket Mode URL but does not connect. Opening the connection is `jotflow run` (#8).

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Token checks in `slack_auth.py` with a fake-friendly interface
- [x] `.env` reader and writer
- [x] Interview: bot name, manifest, link, tokens, `.env`, re-run behaviour
- [x] After #64 merges: channels, repos, agent preset, and `jotflow.toml`
- [x] Add tests
- [x] Update documentation: `manifests/README.md`, the Configuration section of architecture.md, AGENTS.md layout. The README quickstart is #10.
- [x] CodeRabbit review, once: skipped for the rate limit, see Validation
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- 2026-10-18, before #64: `make check` green, 97 tests. New: `tests/test_slack_auth.py`, `tests/test_envfile.py`, `tests/test_init.py`, with shared fakes in `tests/fakes.py`. Init sessions are scripted end to end with a fake Slack client; no network.
- 2026-10-18, after rebasing on #64: `make check` green, 184 tests. New: `tests/test_policy_writer.py` round-trips `jotflow.example.toml` and awkward strings; `tests/test_init.py` covers the policy interview, pasted links, duplicates, re-runs with keep, rewrite, and backup, invalid existing files, and stopping part-way.
- CodeRabbit: skipped. The local CLI hit its free-allowance rate limit (all 3 included reviews used, reset in about 26 minutes). In its place I reviewed the diff myself and fixed two things: a rewrite now says that channels left out are removed, and the owner-only file mode is set only where `os.fchmod` exists, which is not on Windows before Python 3.13.
- 2026-10-18, after rebasing on #66, #68 and #69: `make check` green, 233 tests. New tests cover the policy file mode, the unreadable-policy warning, and `COMPOSE_FILE` being written once and a custom value being kept.
- Security review of #67: 6 items, all taken. (1) The bot token could follow a redirect to another host, because urllib copies request headers onto redirects. `Authorization` is now an unredirected header, and the opener refuses redirects outright, since Slack's Web API never redirects. A test drives urllib's own redirect handling with a local stub. (2) `HttpSlackApi` refuses a non-https base URL. (3) Responses are capped at 1 MB. (4) `jotflow.toml.bak` is gitignored. (5) `.env` and `jotflow.toml` are flushed and fsynced before the atomic replace. (6) `COMPOSE_FILE` was already in; init's closing output now mentions it.
- Found while testing the redirect fix: the first version of that test let urllib's real HTTPS handler run before the stub and reach slack.com. The stub now runs first, and the auth and init tests pass with sockets blocked.
- Found while testing: uppercasing a typed channel id would have accepted `general` as `GENERAL`, a valid-looking private channel id. Ids are now taken as typed.

## Handoff notes

None yet.
