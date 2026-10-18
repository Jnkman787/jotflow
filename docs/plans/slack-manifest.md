# Slack app manifest

## Links and status

- Issue: #5
- Draft PR: #61
- Status: completed
- Last verified: `make check` green locally on 2026-10-18 (ruff, mypy strict, 32 tests); CI on #61
- Next safe action: in the sandbox workspace, create an app from `manifests/slack-app.json` and check the two live criteria on #5

## Objective and done-when

Each team creates its own Slack app for JotFlow by pasting a manifest into Slack. This work adds a renderer that produces that manifest for a chosen bot name, and a checked-in default rendered with the name "JotFlow". `jotflow init` (#7) will call the renderer later.

Done when:

- [x] `render_manifest(bot_name)` returns a manifest with exactly the bot scopes in the architecture Permissions section for v1: `channels:history`, `groups:history`, `chat:write`, `reactions:read`, `reactions:write`, `users:read`, `commands`
- [x] Socket Mode is on, and event subscriptions cover `message.channels`, `message.groups`, and `reaction_added`
- [x] The `/jotflow` slash command is declared
- [x] Interactivity is on, so Approve, Request Changes, and dialogs can reach JotFlow over Socket Mode
- [x] The app is not distributable: org-wide deploy is off and no distribution settings are declared
- [x] A custom bot name is applied to the app name and the bot user, and invalid names are rejected with a clear message
- [x] `manifests/slack-app.json` is checked in, documented, and a test fails if it drifts from the renderer
- [x] `make check` passes

Stays open on #5 after this PR, because it needs the live sandbox workspace:

- Creating an app from the rendered manifest succeeds without edits
- The installed bot can read a message in a private channel it is a member of

**Out of scope:** `app_mentions:read` and the `app_mention` event, which arrive with questions in M5 (D12). `jotflow init` itself (#7). Uploading an icon, which Slack does not accept through a manifest.

## Design

- New module `src/jotflow/slack_manifest.py`, standard library only.
  - `validate_bot_name(name) -> str` strips surrounding whitespace and enforces 1 to 35 characters of letters, digits, spaces, `.`, `-`, and `_`, starting with a letter or digit. Slack caps the app name at 35 characters, and the same value is used for the bot user, so the stricter limit applies to both.
  - `render_manifest(bot_name="JotFlow") -> dict` builds the manifest.
  - `render_manifest_json(bot_name) -> str` returns stable, indented JSON with a trailing newline, so the checked-in file can be compared byte for byte.
  - `python -m jotflow.slack_manifest [name]` prints the JSON. `make manifest` regenerates the checked-in default.
- JSON rather than a YAML template: Slack accepts both, JSON needs no new dependency, and a Python renderer validates the name instead of substituting raw text into a template.
- Scopes and events come from architecture.md Permissions and Intent keywords (D10 reactions, D7 optional reaction approval). Each event's required scope is already in the scope list.
- Not touched: `config.py`, `models.py`, `cli.py`, which other work is changing.

## Risks and open questions

- Slack's exact rules for bot names are not fully documented. The validation is deliberately conservative; loosening it later is safe, tightening it would break existing configs.
- Slack has no manifest field that turns public distribution on, so "not distributable" is guaranteed by declaring `org_deploy_enabled: false` and no distribution settings. The live check on #5 confirms the app installs as internal.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Renderer and name validation in `src/jotflow/slack_manifest.py`
- [x] Checked-in default `manifests/slack-app.json` and `manifests/README.md`
- [x] `make manifest` target
- [x] Add tests
- [x] `make check` passes
- [x] Update AGENTS.md layout and commands
- [x] Mark the PR ready for review

## Validation

- `make check`: ruff lint and format, mypy strict, and pytest all pass; 32 tests, 18 of them new in `tests/test_slack_manifest.py`.
- `make manifest` regenerates `manifests/slack-app.json`; `test_checked_in_manifest_matches_renderer` guards against drift.
- Not yet done: creating an app from the manifest in the sandbox workspace and reading a private channel. Both stay open on #5.

Deviations from the issue text: the manifest is JSON from a Python renderer instead of `manifests/slack.yaml.j2`, which the issue allowed as "or equivalent". Interactivity is turned on, which the issue did not list, because the Approve and Request Changes buttons and the dialog need it.

## Handoff notes

`jotflow init` (#7) should call `render_manifest_json(bot_name)` and surface `BotNameError` messages to the user. When questions land in M5 (D12), add `app_mentions:read` to `BOT_SCOPES` and `app_mention` to `BOT_EVENTS`, update the tests that assert their absence, and run `make manifest`.
