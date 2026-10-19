# Deployment guide

## Links and status

- Issue: #40, the deployment guide only. Security's threat model is merged as `docs/threat-model.md` (#178), and the agent adapter guide is the architect's.
- Also serves: #87 (the no-clone install), #39 (the images), #162 (the health signal), #126 (secrets and rotation), D2, D4, D6
- Draft PR: #183
- Status: ready for review
- Last verified: 2026-10-19. The backup and restore commands were run against a locally built JotFlow image
- Next safe action: review

## Objective and done-when

The README's quickstart gets a team running on one machine. This guide is for whoever keeps JotFlow running after that: on a server, across upgrades, with backups, and beyond plain Compose.

Done when:

- [x] `docs/deployment.md` exists, and the README, the threat model, and architecture.md's Deployment section link to it
- [x] Every command in it was run, or checked against the code, and the guide says which ones need a published release
- [x] It covers:
  - the host
  - secrets and their rotation
  - the data volume, with backup and restore
  - upgrades and rollback
  - health and logs
  - running without Docker
  - what any other orchestrator must keep
- [x] It never repeats the threat model or architecture.md, and links to them instead
- [x] `make check` passes

**Out of scope:** the agent adapter guide, which is the architect's, and `jotflow rebuild` (#38), which isn't built yet. The guide says restoring a backup is the only recovery for now.

## Outline

1. **What runs where.** JotFlow, and the two agent containers once a preset is set up. There are no inbound ports, since Socket Mode is outbound only. Outbound traffic goes to Slack, GitHub, and the model API. Which container needs which.
2. **A server install.** A Linux host with Docker Engine and Compose 2.24 or newer, and `uvx jotflow init` in a dedicated folder. The guide covers starting at boot through Docker's own service and `restart: unless-stopped`, and pinning the image version.
3. **Files and secrets.** `.env` and the agent env files are 0600, and `jotflow.toml` is 0644. What each holds, and never committing them. Rotating the runner secrets with `jotflow init --rotate-runner-secret` and `--rotate-exec-secret`. Revoking a leaked Slack token, by linking manifests/README.md.
4. **Data.** The `jotflow-data` volume holds one SQLite database in WAL mode with `secure_delete`. What's in it is in the threat model, linked. An online backup uses Python's `sqlite3` backup API inside the running container, since the image has no `sqlite3` binary. The guide also covers restoring, and why a plain file copy while running can be torn.
5. **Upgrades and rollback.** Re-run `uvx jotflow@X.Y.Z init` to re-pin, or set `JOTFLOW_IMAGE_REF`. Migrations run at start. A database from a newer JotFlow is refused, so rolling back means restoring the backup taken before the upgrade. A Slack manifest change needs a reinstall, as #170 shows.
6. **Health and logs.** `docker compose ps` shows `healthy` from `jotflow health` (#162). Logs never hold message text, and `--log-bodies` is refused in the image. The guide covers the live check, and where to look first.
7. **Beyond Compose.**
   - Without Docker: `uv tool install jotflow` and `jotflow run` under a systemd unit, with an example.
   - The agent containers' isolation (D6) is the point of running them in containers, so the guide recommends keeping them in Compose.
   - For Kubernetes or another orchestrator: not supported in v1, and here is what any setup must keep. That's separate networks, no published ports, a read-only root, all capabilities dropped, each env file only in its own container, and the database on a persistent volume with one writer.
   - One deployment per Slack workspace.
8. **A short operating checklist**, linking to the threat model's accepted gaps.

## Risks and open questions

- Some steps need a published release, such as `uvx` and pinned images. The guide says so where it matters, as the README does.
- The backup command must be tested against the real image. I'll run it against a locally built image before marking this ready.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective (the deployment guide part), testable done-when
- [x] Commit this plan and open the draft PR
- [x] Write `docs/deployment.md`
- [x] Run the backup and restore commands against a local image, and the systemd unit's command
- [x] Links from the README, the threat model, and architecture.md
- [x] CodeRabbit once: one finding, about this plan's stale status lines, fixed
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- The backup was run against a locally built JotFlow image, in a container holding the database open with a write still in the WAL. It used the online backup, then `cat` out, then `rm`. The copy passed `PRAGMA integrity_check` and held both rows.
- The restore was run through the image's own user, with stdin from a 0600 backup. The restored file is owned by `jotflow`, mode 0600, and has the backed-up rows. A restore by bind mount would have needed the backup made world-readable, so the guide uses stdin.
- Checked against the code:
  - the image has Python and no `sqlite3` program
  - `/data/jotflow.db`, WAL, and `secure_delete`
  - a migration runs in one transaction, and a newer database is refused (`SchemaVersionError`)
  - `jotflow health` and the 30 s heartbeat
  - `--log-bodies` and `check --live` refused in the image
  - `.env` and `jotflow.toml` read from the working folder
  - the rotate flags, `COMPOSE_PROFILES`, the UIDs, `read_only`, `cap_drop`, and the tmpfs for runs
- Not run: the `uvx` commands, which need the first release, as the guide says, and the systemd unit, which follows the command `jotflow run` already takes.

## Handoff notes

- When the first release (#41) is tagged, run the guide's `uvx` install and upgrade steps once on a clean host, and fix anything they show.
- When `jotflow rebuild` (#38) lands, add it to Data, backups, and restore as the recovery for a lost database.
