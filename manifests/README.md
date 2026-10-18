# Slack app manifest

`slack-app.json` is the default manifest for creating a team's own JotFlow app, rendered with the bot name JotFlow. The app stays internal to your workspace and is never distributed.

**Which file to use.** If you run `uv run jotflow init`, use the link it prints, or the `slack-app-manifest.json` it saves in the repository folder: both carry the bot name you chose. Use `manifests/slack-app.json` only to create the app without setup, and then the bot is called JotFlow until you rename it, or render your own as described under [Use a different bot name](#use-a-different-bot-name). See the Permissions section of [docs/architecture.md](../docs/architecture.md) for why JotFlow needs its own app.

## Create the app

1. Go to <https://api.slack.com/apps>, choose **Create New App**, then **From a manifest**.
2. Pick your workspace, click **Next** if Slack shows it, and paste the manifest: `slack-app-manifest.json` if setup saved one, otherwise `slack-app.json`. Slack accepts JSON.
3. Create the app. Under **Basic Information**, in **App-Level Tokens**, click **Generate Token and Scopes**, type any name, click **Add Scope**, choose `connections:write`, click **Generate**, then **Copy**. It starts with `xapp-`, and Socket Mode uses it to connect.
4. Open **Install App**, click **Install to Workspace**, then **Allow**. Copy the **Bot User OAuth Token** shown on that page; it also appears under **OAuth & Permissions**. It starts with `xoxb-`. If Slack shows **Request to Install**, an admin must approve the app first; the main README explains what to do while you wait.
5. Set an icon under **Basic Information** if you like. Slack does not take icons from a manifest.
6. Invite the bot to each channel JotFlow should watch with `/invite @YourBotName`. The bot never joins channels by itself, and `jotflow run` reports configured channels it has not been invited to.

JotFlow needs both tokens. Keep them secret and out of the repository. Token rotation is off, so a leaked token stays valid until you revoke it. If one leaks, choose **Revoke All OAuth Tokens** on **OAuth & Permissions**, regenerate the app-level token under **Basic Information**, and reinstall the app. `jotflow init` wraps these steps: it renders the manifest with your bot name, prints a link that opens Slack's create-app dialog with it filled in, checks both tokens, and writes them to your `.env`.

## Add a missing scope

If JotFlow or setup reports a missing scope, open the app's **OAuth & Permissions** page, scroll to **Scopes**, and under **Bot Token Scopes** click **Add an OAuth Scope** for each scope named. Slack then shows a banner: click **Reinstall to Workspace** and **Allow**. Reinstalling can issue a new bot token, so run `uv run jotflow init` again and paste it.

## Use a different bot name

Render the manifest with your own name instead of editing the file by hand:

```
uv run python -m jotflow.slack_manifest "Idea Bot"
```

A name has at most 35 characters: letters, digits, spaces, `.`, `-` and `_`, starting with a letter or digit.

## What the manifest asks for

| Setting | Value | Why |
|---|---|---|
| Bot scopes | `channels:history`, `groups:history` | Read messages in public and private channels the bot is in |
| | `channels:read`, `groups:read` | Confirm the bot is in each configured channel, and detect channels shared with other organisations |
| | `chat:write` | Post proposal cards, questions, and issue links |
| | `reactions:read`, `reactions:write` | Keyword reactions and their acknowledgements, optional reaction approval |
| | `pins:write` | Pin a short help message when JotFlow joins a channel |
| | `users:read` | Show names on cards and skip guests and external users |
| | `commands` | The `/jotflow` slash command |
| Events | `message.channels`, `message.groups`, `reaction_added` | Wake JotFlow when someone posts or reacts |
| Socket Mode | on | No public URL is needed for a self-hosted install |
| Interactivity | on | Approve and Request Changes buttons and dialogs |
| App Home tabs | off | The app has no DM scopes, so messages sent to it would never be read |
| Org-wide deploy | off | The app is internal to one workspace |

There is no `channels:join` scope: joining a channel is always a team member's explicit invite. Questions to the bot by @-mention arrive in M5 and will add `app_mentions:read`.

## Changing the manifest

Edit `src/jotflow/slack_manifest.py`, then regenerate this file:

```
make manifest
```

A test fails if `slack-app.json` and the renderer disagree.
