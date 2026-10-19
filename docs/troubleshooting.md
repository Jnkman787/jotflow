# Troubleshooting

What to check when JotFlow, setup, or Slack doesn't do what [install.md](install.md) says.

Running JotFlow on a server, with backups, upgrades, and rollback, is covered in [docs/deployment.md](deployment.md). Adding support for another agent CLI, or checking a custom agent command, is covered in [docs/agent-adapters.md](agent-adapters.md).

**Is JotFlow still connected?** `docker compose ps` shows `healthy` while JotFlow is connected to Slack, and `unhealthy` within about three minutes of losing it: 90 seconds for the heartbeat to go stale, then three failed checks 30 seconds apart. JotFlow rewrites a small heartbeat file every 30 seconds while connected, and `jotflow health` checks its age, without the network or any token. Its log shows `socket mode disconnected`, then `socket mode reconnecting (attempt N)` every 30 seconds, then `socket mode reconnected after Ns`. Slack's client reconnects by itself; the attempt count is JotFlow's own checks. To alert, watch for the container turning `unhealthy`: for example, `docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q jotflow)"` from cron, or your monitor's Docker health integration. Outside Docker, run `uvx jotflow health` on the same machine. It exits 0 when healthy and 1 otherwise, and `--max-age 120` changes how old the heartbeat may be.

**Check a fresh install** with the live check. It posts one marked test message to a channel in `jotflow.toml`, checks that JotFlow receives it without logging its text, then deletes it.

1. Stop JotFlow, because Slack gives each message to only one connection, so a running JotFlow would take the test message: `docker compose stop jotflow`, or press Ctrl-C in its terminal if you started it with `uvx jotflow run`.
2. Run the check. It asks before posting; answer `y`.
   - macOS, Linux, or WSL: `JOTFLOW_LIVE_CHECK=1 uvx jotflow check --live`
   - PowerShell: `$env:JOTFLOW_LIVE_CHECK = "1"; uvx jotflow check --live; Remove-Item Env:JOTFLOW_LIVE_CHECK`
3. Read the result. Each line says `ok` or `FAIL`, and a `FAIL` says what to fix.
4. Start JotFlow again: `docker compose start jotflow`

`JOTFLOW_LIVE_CHECK=1` is how you say yes to posting, for that one command only. Never put it in `.env`; the check refuses if it's there. If several channels are listed, pick one with `--channel C0IDEAS01`. Without uv, use `.venv/bin/jotflow`, or `.venv\Scripts\jotflow` on Windows, in place of `uvx jotflow`. In PowerShell, if you stop the check with Ctrl-C, close the window afterwards so the setting does not linger. `uvx jotflow check`, without `--live`, only checks `.env` and `jotflow.toml` and posts nothing.

Every error names what to fix. The common ones are below. Wherever it says `uvx jotflow init`, use `.venv/bin/jotflow init` if you set up without uv.

| You see | What to do |
|---|---|
| `jotflow init is interactive; run it in a terminal.` | Run setup by typing it in a terminal window, not from a script or another program. |
| `uv: command not found`, or uv is not recognized | Open a new terminal after installing uv, so it is found. |
| `ERROR: Package 'jotflow' requires a different Python` | The Python you used is older than 3.12. Install 3.12 or newer from <https://www.python.org/downloads/>, or use uv, which fetches it for you. |
| In WSL, `The command 'docker' could not be found` | In Docker Desktop, open **Settings**, **Resources**, **WSL integration**, switch it on for your distribution, and click **Apply & restart**. |
| `permission denied while trying to connect to the Docker daemon socket` | On Linux, your user isn't allowed to run Docker yet. Run `sudo usermod -aG docker $USER`, then log out and back in. |
| `That is an app-level token (xapp-).` or `That is a bot token (xoxb-).` | The two tokens were swapped. Paste the other one. |
| `The bot token is missing these scopes: ...` or `missing this scope` | The app wasn't created from JotFlow's manifest, or the manifest changed. In the app's settings, open **OAuth & Permissions**, scroll to **Scopes**, and under **Bot Token Scopes** click **Add an OAuth Scope** for each scope named. Slack then shows a banner: click **Reinstall to Workspace** and **Allow**. Setup asks for the bot token again straight away, so copy the new **Bot User OAuth Token** and paste it; you have five tries. If setup has already stopped, run `uvx jotflow init` again. |
| `The app-level token is missing the connections:write scope.` | Generate a new app-level token under **Basic Information**, with the `connections:write` scope. |
| `This token has expired, which means token rotation is on for the app.` | Token rotation is Slack's option to replace tokens every few hours, and JotFlow needs tokens that last. Slack does not let you turn it off once it is on, so create the app again from the link setup prints, run setup again, and delete the old app under **Basic Information**. [manifests/README.md](../manifests/README.md) has the details. |
| `Slack does not recognise this token.` | The token was copied incompletely. Copy it again with the **Copy** button. |
| `That is the channel's name. Setup needs its id` | You typed a name such as `#ideas`. Open the channel, click its name, and copy the **Channel ID** from the bottom of the **About** tab. |
| `... is not a Slack channel id such as C0123ABCD` | The id was mistyped. It is a capital `C` or `G` followed by capital letters and digits, such as `C0IDEAS01`. Setup refuses an id typed in lowercase and shows the capitalised form to check, but never guesses. Copy the id exactly as Slack shows it, or paste the channel's link. |
| `could not reach Slack` | Check your network. If your workplace requires a proxy, a server all web traffic goes through, ask your IT team for its address and set it in the terminal before running setup: `export HTTPS_PROXY=http://proxy.acme-co.example:8080`, or in PowerShell `$env:HTTPS_PROXY = "http://proxy.acme-co.example:8080"`. That covers setup only. For JotFlow running in Docker, also add the same line, `HTTPS_PROXY=http://proxy.acme-co.example:8080`, to `.env`, which Docker passes to JotFlow, then run `docker compose up -d` again. |
| `jotflow.toml has problems:` followed by setting names | A hand edit broke the file. Each line names the setting at fault. Fix it, or let setup write a new one. The old file is kept as `jotflow.toml.bak`. |
| `configuration problem in environment or .env:` `JOTFLOW_SLACK_BOT_TOKEN: not set` | `.env` is missing or isn't next to `docker-compose.yml`. Run `uvx jotflow init` in your JotFlow folder. |
| `configuration problem in jotflow.toml:` ``file not found; run `jotflow init` to create it`` | The container didn't get your `jotflow.toml`. Check that `.env` has the `COMPOSE_FILE` line. If you removed it, start with `docker compose -f docker-compose.yml -f docker-compose.policy.yml up -d`. |
| `Cannot connect to the Docker daemon` | Docker isn't running; the daemon is its background service. Start Docker Desktop, or on Linux `sudo systemctl start docker`, wait until it is running, and try again. |
| Compose says the bind source path for `jotflow.toml` does not exist | The bind source path is the file on your machine that Docker hands to JotFlow. Docker was asked to hand `jotflow.toml` to JotFlow, but the file isn't there yet. Run `uvx jotflow init` before `docker compose up -d`. |
| `jotflow.toml` is a directory, or JotFlow reports `Is a directory` | Compose ran before setup, and Docker Desktop created an empty directory in place of the missing file. Stop JotFlow with `docker compose down`. Then remove the directory: `rm -r jotflow.toml`, or `sudo rm -r jotflow.toml` if that is refused, because Docker created it as root, the administrator account. In PowerShell: `Remove-Item -Recurse jotflow.toml`. Then run `uvx jotflow init` and start again with `docker compose up -d`. |
| `Permission denied` on `/etc/jotflow/jotflow.toml` | JotFlow's container runs as its own user and must be able to read the file. Make it readable with `chmod 644 jotflow.toml`; `chmod` changes a file's permissions, and `644` lets everyone read it and only you change it. |
| `not a member of channel C0IDEAS01; invite the bot there with /invite @idea_bot` | Invite the bot to that channel. JotFlow keeps running and watches the channels it is in. |
| `could not look up channel ... the app needs the channels:read and groups:read scopes` | The app was created from an older manifest. Add the two scopes as described for missing scopes above, reinstall the app, and run `uvx jotflow init` again to save the new bot token. |
| `could not listen on 127.0.0.1:8765; is the port in use?`, and `jotflow run` stops | A port is a numbered connection point on your machine. JotFlow uses port 8765 for your agent to connect to, and something else, often a second JotFlow, already holds it. Stop the other one, or pick another port by adding a line such as `JOTFLOW_MCP_PORT=8766` to `.env`. |
| `Slack rejected the bot token (...)` or `Slack refused the Socket Mode connection (...)` | A token was revoked or the app was reinstalled. Run `uvx jotflow init` again to save fresh tokens. |
| `FAIL  receive: no event for the test message` from the live check | First make sure JotFlow is stopped, since a running JotFlow can take the event. Otherwise the app is not getting message events. Check the app's **Event Subscriptions** page, which lists the Slack events it receives, for `message.channels` and `message.groups`, and that Socket Mode is on, then reinstall the app. |
| No `message` lines appear | Check that the channel's id is in `jotflow.toml` and that the bot is in the channel. JotFlow ignores channels it isn't configured for and its own messages. |
| On Windows, Compose ignores the `COMPOSE_FILE` line | `.env` lacks `COMPOSE_PATH_SEPARATOR=:`, which setup adds on Windows. Run `uvx jotflow init` again, or add the line yourself. |
