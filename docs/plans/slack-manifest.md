# Slack app manifest

## Links and status

- Issue: #5
- Draft PR: to be opened
- Status: planning
- Last verified: plan written, no code yet
- Next safe action: open the draft PR, then write `src/jotflow/slack_manifest.py` and its tests

## Objective and done-when

Each team creates its own Slack app for JotFlow by pasting a manifest into Slack. This work adds a renderer that produces that manifest for a chosen bot name, and a checked-in default rendered with the name "JotFlow". `jotflow init` (#7) will call the renderer later.

Done when:

- [ ] `render_manifest(bot_name)` returns a manifest with exactly the bot scopes in the architecture Permissions section for v1: `channels:history`, `groups:history`, `chat:write`, `reactions:read`, `reactions:write`, `users:read`, `commands`
- [ ] Socket Mode is on, and event subscriptions cover `message.channels`, `message.groups`, and `reaction_added`
- [ ] The `/jotflow` slash command is declared
- [ ] Interactivity is on, so Approve, Request Changes, and dialogs can reach JotFlow over Socket Mode
- [ ] The app is not distributable: org-wide deploy is off and no distribution settings are declared
- [ ] A custom bot name is applied to the app name and the bot user, and invalid names are rejected with a clear message
- [ ] `manifests/slack-app.json` is checked in, documented, and a test fails if it drifts from the renderer
- [ ] `make check` passes

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
- [ ] Commit this plan and open the draft PR
- [ ] Renderer and name validation in `src/jotflow/slack_manifest.py`
- [ ] Checked-in default `manifests/slack-app.json` and `manifests/README.md`
- [ ] `make manifest` target
- [ ] Add tests
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
