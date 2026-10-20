# Deploying JotFlow

The [install guide](install.md) gets JotFlow running on one machine. This guide is for whoever keeps it running after that: on a server, through upgrades and restores, and beyond plain Docker Compose.

It links rather than repeats. What JotFlow protects, and what it deliberately doesn't, is in the [threat model](threat-model.md). How the parts fit together is in [architecture.md](architecture.md).

## What runs, and what it talks to

| Container | Holds | Talks to |
|---|---|---|
| `jotflow` | `.env`: the Slack tokens and both runner secrets. The `jotflow-data` volume | Slack, over a websocket JotFlow opens. The two runners, on internal networks |
| `agent` | `agent.env`: the read-only GitHub token and the model login | JotFlow's MCP server, GitHub, and the model API |
| `agent-exec` | `agent-exec.env`: the GitHub token that can write issues, and the model login | JotFlow's MCP server, GitHub, and the model API |

The two agent containers start only once `COMPOSE_PROFILES=agent` is set, which `jotflow init` does when their GitHub tokens are filled in.

Nothing listens for inbound connections. Slack's Socket Mode is a connection JotFlow makes outward, so the host needs no open port, public address, or reverse proxy. It needs outbound HTTPS to Slack, to GitHub, and to the model API the agents use.

**DNS search domains (#234).** The containers find each other by Compose service name: `jotflow`, `agent` and `agent-exec`. A name whose service isn't running, such as `agent-exec` with only drafting on, is unknown to Docker's DNS. A lookup would then try the host's DNS search domain, and some ISP or DHCP resolvers answer every unknown name with a public address. So:
- The Compose files set `dns_search: ["."]` on every service. A missing service name then fails to resolve instead.
- JotFlow sends a runner secret, and a run token, only to a runner whose name resolves to private, loopback or link-local addresses: 10/8, 172.16/12, 192.168/16, 100.64/10, 127/8, 169.254/16, ::1, fc00::/7 or fe80::/10. It connects to the address it checked. A refused runner's kinds stay off, and the log says why.
- Each runner refuses a run unless `jotflow`, the MCP server's host, resolves the same way, since the agent sends its run token there.
- A runner on another machine works over a private network or a VPN such as Tailscale, which uses 100.64/10, with nothing to set. To send secrets to a runner at a public address anyway, set `JOTFLOW_RUNNER_ALLOW_PUBLIC=1` in `.env`. For a runner that reaches JotFlow at a public address, set `JOTFLOW_MCP_ALLOW_PUBLIC=1` in that agent's env file. Both say so at startup, and both send secrets over plain HTTP, so prefer the VPN.

## A server install

Use one Linux host with Docker Engine and Docker Compose 2.24 or newer. Give JotFlow a folder of its own that only its operator can read, for example `/srv/jotflow`.

```
sudo install -d -m 0700 -o "$USER" /srv/jotflow
cd /srv/jotflow
uvx jotflow init
docker compose up -d
```

`uvx jotflow init` writes `docker-compose.yml` and `docker-compose.policy.yml` pinned to the release you ran, for example `ghcr.io/jnkman787/jotflow:1.2.3`. It works from JotFlow's first published release (#41). Before that, use [Run from a checkout](../CONTRIBUTING.md#run-from-a-checkout).

- **Starting at boot.** The containers use `restart: unless-stopped`, so they come back whenever Docker does. Enable Docker's own service: `sudo systemctl enable --now docker`. No unit file of your own is needed.
- **Pinning.** The images are pinned by version, never `:latest`, so a restart never changes what runs. To run another image, set `JOTFLOW_IMAGE_REF` and `JOTFLOW_AGENT_IMAGE_REF` in `.env`. Not `JOTFLOW_IMAGE`, which is a marker inside JotFlow's own image.
- **One deployment per Slack workspace.** A Slack app belongs to one workspace, and JotFlow holds one app's tokens.

## Files and secrets

| File | Mode | Holds |
|---|---|---|
| `.env` | 0600 | Slack bot and app tokens, both runner secrets, `COMPOSE_FILE` and `COMPOSE_PROFILES` |
| `agent.env` | 0600 | Drafting runner's secret, read-only GitHub token, model login |
| `agent-exec.env` | 0600 | Execution runner's secret, GitHub token that writes issues, model login |
| `jotflow.toml` | 0644 | Channels, repos, labels, and approvers. No secrets |

`jotflow init` writes them all atomically with those modes. `jotflow.toml` is world-readable on purpose: the container reads it as its own user. JotFlow reads it only when it starts, and the container mounts it read-only, so after editing it run `docker compose restart jotflow`. `docker compose up -d` alone doesn't restart a container when only a mounted file changed. Never commit any of them, and keep them out of anything that syncs to the cloud.

**Rotating a runner secret**, for example after a host was shared:

```
uvx jotflow init --rotate-runner-secret     # the drafting runner's
uvx jotflow init --rotate-exec-secret       # the execution runner's
docker compose up -d
```

Setup writes `.env` and the agent file together, so the two can't be left disagreeing. The restart lets every container pick up the new value.

**A leaked Slack token** is revoked in Slack itself, which [manifests/README.md](../manifests/README.md) explains. Then run `uvx jotflow init` again to save the new one. A leaked GitHub token is revoked on GitHub, then replaced in its agent env file, followed by `docker compose up -d`.

## Data, backups, and restore

JotFlow keeps one SQLite database, `/data/jotflow.db`, on the `jotflow-data` volume, in WAL mode with `secure_delete` on. It holds pointers and decisions, not thread text. Drafts stay only until a proposal is final. The threat model's [Data JotFlow keeps](threat-model.md#data-jotflow-keeps) lists exactly what's kept. A copy of the database still holds live drafts, so treat backups as private.

**Back up while JotFlow runs.** SQLite's online backup copies a consistent snapshot, including writes still in the WAL. A plain file copy of a running database can be torn. The image has Python but no `sqlite3` program, so:

```
docker compose exec -T jotflow python -c "import sqlite3; s = sqlite3.connect('file:/data/jotflow.db?mode=ro', uri=True); d = sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
(umask 077; docker compose exec -T jotflow cat /data/backup.db > "jotflow-$(date +%F).db")
docker compose exec -T jotflow rm /data/backup.db
```

The backup file is created readable only by you. Keep at least the one from before each upgrade.

**Restore** by stopping JotFlow, replacing the database through the image's own user, and starting again:

```
docker compose stop jotflow
docker compose run --rm -T --no-deps jotflow sh -c 'rm -f /data/jotflow.db-wal /data/jotflow.db-shm && umask 077 && cat > /data/jotflow.db' < jotflow-2026-10-19.db
docker compose up -d
```

The old database's WAL files are removed first, so none of their pages are replayed onto the restored file. The restored file is owned by the container's user with mode 0600, as JotFlow writes it.

A restore loses whatever happened after the backup was taken.

### Rebuild from Slack, without a backup

`jotflow rebuild` restores the part of the store that routes new messages: which threads and messages belong to which GitHub issue. It reads JotFlow's own replies in each configured channel, the `Created` and `Updated` messages with an issue link, and writes only pointers. It never writes message text, and it doesn't touch GitHub. Run it with JotFlow stopped:

```
docker compose stop jotflow
docker compose run --rm --no-deps jotflow jotflow rebuild
docker compose start jotflow
```

It restores:
- **a topic per issue**, titled by its nickname and number, such as `web#42`
- **the people's messages** in each such thread, linked to their topic, or marked as not an idea when they start with `skip:`
- **each channel's mode,** from JotFlow's latest mode announcement
- **whether the channel's guide was already posted,** so it isn't posted and pinned twice

It prints progress per channel, then counts. A big channel takes minutes, because Slack limits how fast history can be read, and rebuild waits out each limit.

**Options.**
- `--dry-run` reads Slack and prints what it would restore, writing nothing.
- `--since 90d` reads only the last 90 days. The default is all history.
- `--channel C0IDEAS01` rebuilds one channel, and can be repeated.
- `--merge` adds to a store that already has topics, never overwriting what's there. Without it, rebuild refuses a store with topics in a configured channel.

**What it can't restore.** These are by design:
- proposals still pending, approved or failed. Their drafts are gone, and the next message in the thread drafts again.
- applied proposals' history: who approved, and the run records
- identity links, operator tokens and identity unlinks, which Slack only showed privately. People run `/jotflow link` again, and admins reissue tokens.
- old reaction marks
- real topic titles
- messages deleted in Slack, threads older than the workspace's history retention or `--since`, and threads JotFlow never wrote an issue for

**The running check.** Rebuild refuses when it sees JotFlow's heartbeat, but a separate `docker compose run` container can't see the running one's, so stop JotFlow first.

## Upgrading, and rolling back

1. Read the release notes. They say when a release needs a Slack app update. For example, the release that added `reaction_removed` needs the new manifest and a reinstall, as [manifests/README.md](../manifests/README.md) explains. Render the new manifest with your bot's name and the release you're upgrading to, `uvx --from jotflow@X.Y.Z python -m jotflow.slack_manifest "Your Bot Name"`. The stock `slack-app.json` resets the bot's name to JotFlow, and setup's saved `slack-app-manifest.json` may predate the change.
2. Take a backup, as above.
3. Re-pin, from the JotFlow folder:

   ```
   uvx jotflow@1.3.0 init
   docker compose up -d
   ```

   Setup keeps your tokens, `jotflow.toml`, and secrets. It then asks before replacing each Compose file with the new release's: answer `y`. The old file is kept as a `.bak`, so any change you'd made to it can be carried over. Setting `JOTFLOW_IMAGE_REF` and `JOTFLOW_AGENT_IMAGE_REF` in `.env` to the new version works too.

Database migrations run when JotFlow starts, each in one transaction, so a failed migration leaves the previous version intact. JotFlow refuses a database written by a newer JotFlow, rather than guessing. So **rolling back means restoring the backup from before the upgrade**, then pinning the older version again.

## Health and logs

- **Is it connected?** `docker compose ps` shows `healthy` while JotFlow is connected to Slack. The health check, `jotflow health`, reads a heartbeat that `jotflow run` rewrites every 30 seconds while connected, so a lost connection shows `unhealthy` within a few minutes. Point any monitoring at that status.
- **Logs** go to Docker: `docker compose logs -f jotflow`. They hold ids, timestamps, and reasons, never message text. Every line passes one redacting formatter that also removes every token shape JotFlow knows. `--log-bodies` is refused inside JotFlow's image, because Docker keeps its logs.
- **Is the Slack app set up right?** The [live check](troubleshooting.md) posts one test message and confirms JotFlow hears it. It runs from the JotFlow folder on the host, not in the container, which refuses it.

## Beyond Compose

**JotFlow without Docker**, for trying it out or development. JotFlow runs as an ordinary program. What that gives up:

- **No agent runs.** The agent containers are what keep the GitHub write token away from drafting runs, which read untrusted Slack text (D6). Without them there's no runner to call, so agent runs stay off: JotFlow connects, reads keywords, and answers commands, and nothing is drafted or written.
- **JotFlow's own container limits.** These are a read-only root filesystem, every Linux capability dropped, no privilege escalation, its own user, and internal networks that reach only the runners, plus outbound internet for Slack's connection. The systemd settings below put back what a unit can: no new privileges, a private `/tmp`, and a read-only system apart from JotFlow's folder. They don't separate JotFlow's network from the rest of the host.
- **The files' isolation.** `.env` and `jotflow.db` sit in a folder that any process running as the same user can read. Run JotFlow as its own user, with that folder at mode 0700.

Don't run the agent's runner outside its container to get agent runs back. Its isolation from JotFlow's secrets and from the other runner is the defence, not a detail of packaging.

If you run `jotflow runner` yourself on the same host, outside separate containers, you lose the token split. Processes running as the same user can read each other's environment through `/proc`, so both GitHub tokens, the read-only one and the one that can write, are exposed to every run, including drafting runs that read untrusted Slack text. Even inside the containers, runs of the same kind can read each other this way. The threat model accepts that only because they hold the same token: see [Accepted in v1](threat-model.md#accepted-in-v1) and #181.

Under systemd:

```
[Unit]
Description=JotFlow
After=network-online.target
Wants=network-online.target

[Service]
User=jotflow
WorkingDirectory=/srv/jotflow
ExecStart=/home/jotflow/.local/bin/jotflow run
Restart=on-failure
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/srv/jotflow

[Install]
WantedBy=multi-user.target
```

Install it as that user with `uv tool install jotflow`. If `uv tool dir --bin` prints a folder other than `/home/jotflow/.local/bin`, use that in `ExecStart`. `jotflow run` reads `.env` and `jotflow.toml` from its working folder, and keeps `jotflow.db` there unless `JOTFLOW_DATABASE_PATH` says otherwise.

**Kubernetes, Nomad, or another orchestrator** is not supported in this release. If you build one, it has to keep what the Compose file does, or it gives up a defence the threat model relies on:

- **Three separate workloads.** Each env file is mounted only into its own container. The write token reaches only `agent-exec`, and nothing from `.env` reaches an agent.
- **Separate networks.**
  - JotFlow reaches each runner, and each runner reaches JotFlow's MCP server.
  - The drafting runner can't reach `agent-exec`.
  - Only JotFlow reaches Slack.
  - No service publishes a port.
  - No DNS search domain, so a missing service name doesn't resolve elsewhere (#234).
- **Hardened containers.** Each has a read-only root filesystem, all capabilities dropped, no privilege escalation, and a non-root user: 10001 for JotFlow, 10002 for the agents. The agents' run directories go on in-memory storage.
- **No Docker socket** in any container.
- **One JotFlow at a time** against the database, on a persistent volume. SQLite with one writer is the design. Two replicas would both answer Slack.

## An operating checklist

- [ ] `.env` and both agent env files are 0600, in a folder only the operator can read
- [ ] The images are pinned to a version, and you know which one is running
- [ ] There's a backup from before the last upgrade, and you've done a restore once
- [ ] `docker compose ps` is monitored for `unhealthy`
- [ ] `docker compose exec jotflow jotflow operator list` shows no token you don't expect; revoke any that's unused
- [ ] With guests, Slack Connect, or wide membership in a channel, approvers are on, and the channel isn't left in `auto` (see the threat model)
- [ ] You've read the threat model's [Accepted in v1](threat-model.md#accepted-in-v1) section
