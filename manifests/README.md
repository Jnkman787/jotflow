# Slack app manifest

A manifest is a file describing a Slack app: its name, permissions, and settings, so Slack can build the app from it. `slack-app.json` is the default manifest for creating a team's own JotFlow app, rendered with the bot name JotFlow. The app stays internal to your workspace and is never distributed.

**Which file to use.** If you run `uvx jotflow init`, use the link it prints, or the `slack-app-manifest.json` it saves in your JotFlow folder: both carry the bot name you chose. Use `manifests/slack-app.json` only to create the app without setup, and then the bot is called JotFlow until you rename it, or render your own as described under [Use a different bot name](#use-a-different-bot-name). See the Permissions section of [docs/architecture.md](../docs/architecture.md) for why JotFlow needs its own app.

## Create the app

1. Go to <https://api.slack.com/apps>, choose **Create New App**, then **From a manifest**.
2. Pick your workspace, click **Next** if Slack shows it, and paste the manifest: `slack-app-manifest.json` if setup saved one, otherwise `slack-app.json`. Slack accepts JSON.
3. Create the app. Under **Basic Information**, in **App-Level Tokens**, click **Generate Token and Scopes**, type any name, click **Add Scope**, choose `connections:write`, click **Generate**, then **Copy**. It starts with `xapp-`, and Socket Mode uses it to connect.
4. Open **Install App**, click **Install to Workspace**, then **Allow**. Copy the **Bot User OAuth Token** shown on that page; it also appears under **OAuth & Permissions**. It starts with `xoxb-`. If Slack shows **Request to Install**, an admin must approve the app first; the main README explains what to do while you wait.
5. Set an icon under **Basic Information** if you like. Slack does not take icons from a manifest.
6. Invite the bot to each channel JotFlow should watch with `/invite` and the bot's handle, for example `/invite @idea_bot`. Setup prints the handle after `Connected to Slack as`, and Slack suggests it as you type. The bot never joins channels by itself, and `jotflow run` reports configured channels it has not been invited to.

JotFlow needs both tokens. Keep them secret and out of the repository. Token rotation, Slack's option to replace tokens automatically every few hours, is off: JotFlow needs tokens that last. So a leaked token stays valid until you revoke it. If rotation was ever turned on for this app, Slack does not let you turn it off again: create a new app from JotFlow's manifest, run setup again, and delete the old app under **Basic Information**. If one leaks, choose **Revoke All OAuth Tokens** on **OAuth & Permissions**, regenerate the app-level token under **Basic Information**, and reinstall the app. `jotflow init` wraps these steps: it renders the manifest with your bot name, prints a link that opens Slack's create-app dialog with it filled in, checks both tokens, and writes them to your `.env`.

## Add a missing scope

**Updating an app created from an older manifest.** The manifest now subscribes to `reaction_removed` (#138), so removing a keyword's reaction can take it back. It also turns on a read-only Messages tab (#173), where JotFlow tells a person their GitHub link was removed. It needs no new scope, people can't write to the app there, and JotFlow reads no DMs. An app made from the older manifest keeps working, but removing a reaction then does nothing, and the removal note can't be delivered, so the person who removed the link is asked to pass it on. To pick both up, render a current manifest with your bot's name, as [Use a different bot name](#use-a-different-bot-name) shows:

```
uvx --from jotflow python -m jotflow.slack_manifest "Idea Bot" > slack-app-manifest.json
```

Then open the app's **App Manifest** page, paste that file, save, and click **Reinstall to Workspace** if Slack asks. Don't paste the stock `slack-app.json` as it is: it sets `display_information.name` and `features.bot_user.display_name` to JotFlow, so it resets your bot's name. Change those two fields first if you use it. An older `slack-app-manifest.json` from setup keeps the name but may lack the change you're picking up, because setup writes it only when it creates a new app.

If JotFlow or setup reports a missing scope, open the app's **OAuth & Permissions** page, scroll to **Scopes**, and under **Bot Token Scopes** click **Add an OAuth Scope** for each scope named. Slack then shows a banner: click **Reinstall to Workspace** and **Allow**. Reinstalling can issue a new bot token. If setup is still asking for the bot token, paste the new one there. If setup has already stopped, run `uvx jotflow init` again and paste it.

## Use a different bot name

`uvx jotflow init` already renders the manifest with the name you choose. To render one without setup, for example to update an existing app, run:

```
uvx --from jotflow python -m jotflow.slack_manifest "Idea Bot"
```

From a checkout, contributors can run `uv run python -m jotflow.slack_manifest "Idea Bot"` instead.

A name has at most 35 characters: letters, digits, spaces, `.`, `-` and `_`, starting with a letter or digit.

## What the manifest asks for

Each permission below is used in this release. JotFlow asks for nothing it doesn't use, and it has no DM scopes.

| Setting | Value | Why |
|---|---|---|
| Bot scopes | `channels:history`, `groups:history` | Read messages in public and private channels the bot is in |
| | `channels:read`, `groups:read` | Confirm the bot is in each configured channel, and detect channels shared with other organisations |
| | `chat:write` | Post proposal cards, questions, and issue links |
| | `reactions:read`, `reactions:write` | Read keyword and approval reactions, and react on the messages an issue came from |
| | `pins:write` | Pin a short help message once per channel |
| | `users:read` | Show names on cards, and keep guests and external users from approving |
| | `commands` | The `/jotflow` slash command |
| Events | `message.channels`, `message.groups`, `reaction_added`, `reaction_removed` | Wake JotFlow when someone posts, reacts, or takes a reaction back |
| Socket Mode | on | No public URL is needed for a self-hosted install |
| Interactivity | on | The card's buttons, repo menu, and dialogs |
| App Home | Messages tab on, read-only; Home tab off | JotFlow tells a person there when their GitHub link was removed. People can't write to the app, and it has no DM scopes, so it reads no DMs |
| Org-wide deploy | off | The app is internal to one workspace |

There is no `channels:join` scope: joining a channel is always a team member's explicit invite. Answering questions by @-mention isn't built yet (#57). When it is, the manifest will add `app_mentions:read`, and the release notes will say to update the app.

## Changing the manifest

This is for contributors, in a checkout. Edit `src/jotflow/slack_manifest.py`, then regenerate this file:

```
make manifest
```

A test fails if `slack-app.json` and the renderer disagree.
