# A health signal for the Slack connection

## Links and status

- Issue: #133
- Draft PR: #162
- Status: ready for review
- Last verified: `make check` green, and the image checks run locally, on 2026-10-19
- Next safe action: reviewer reviews #162

## Objective and done-when

An operator can't tell "running" from "running but disconnected from Slack". This adds connection log lines, a heartbeat file, a `jotflow health` command, and a Compose healthcheck that an operator can alert on.

- [x] A Socket Mode drop and recovery logs three lines at INFO: `socket mode disconnected`, then `socket mode reconnecting (attempt N)`, then `socket mode reconnected after Ns`. It's tested with a fake connection. The lines carry no tokens or ids.
- [x] While connected, the run loop writes the heartbeat file every 30 s. While disconnected, it doesn't, so the file goes stale.
- [x] `jotflow health` exits 0 with one line when the heartbeat is under 90 s old, configurable with `--max-age`, and exits 1 otherwise. It makes no network call and reads no tokens, `.env`, or `jotflow.toml`, so it works on the read-only image. Tests cover fresh, stale, missing, and unreadable files.
- [x] The base `docker-compose.yml` has the healthcheck. CI runs `jotflow health` in the built image with a read-only root and a `/tmp` tmpfs: no heartbeat exits 1, and a fresh one exits 0. A healthy service needs a real Slack connection, so the full transition is unit-tested.
- [x] The README's troubleshooting section explains the signal and how to alert on `unhealthy`.

## Design

**`orchestrator/health.py`** holds three pieces:
- `ConnectionWatch(connection, heartbeat, clock, interval=5 s, beat_every=30 s)` polls `await connection.is_connected()`. The SDK reconnects on its own, and JotFlow only observes it. On a change from connected to disconnected it logs `disconnected`, then `reconnecting (attempt N)` on the first check and every 30 s while it stays down, counting checks. On a change back it logs `reconnected after Ns` and beats at once.
- `Heartbeat(path)` writes the time atomically, to a temp file in the same directory and then `os.replace`, with mode 0600. It never holds a token.
- `read_heartbeat(path, now)` returns the age in seconds, or None.

**The path** is `JOTFLOW_HEARTBEAT_FILE`, default `/tmp/jotflow-heartbeat`. That's the tmpfs in Compose, and a temp directory elsewhere. `jotflow health` reads only that one environment variable, never `Settings`, because `Settings` requires the Slack tokens.

**`serve`** starts the watch as a task after `connect_async` and stops it before closing Slack. The Socket Mode handler's client exposes `is_connected()`, and a fake connection implements the same.

**Compose:**

```yaml
healthcheck:
  test: ["CMD", "jotflow", "health"]
  interval: 30s
  timeout: 5s
  start_period: 60s
  retries: 3
```

## Risks and open questions

- **The attempt count** counts JotFlow's checks, not the SDK's internal tries, which aren't exposed. The README says so.
- **The heartbeat file must be writable.** In the image, `/tmp` is the tmpfs. Outside Compose it's the system temp directory, so `jotflow health` can run on the host too.

## Progress checklist

- [x] `health.py`: the watch, the heartbeat, and reading it, with tests
- [x] `jotflow health` command, with tests
- [x] `serve` wiring, with a fake-connection test for drop and recovery
- [x] Compose healthcheck, and the CI image step
- [x] Docs
- [x] `make check` passes

## Validation

`make check` is green. `tests/test_health.py` covers:

- the path from the environment or tmp;
- a beat being a 0600 time, written atomically with no temp file left, and its age read back, including a clock that stepped back;
- missing and garbled heartbeats;
- a drop and recovery giving exactly the three lines, with reconnecting every 30 s, no beat while down, and an immediate beat on reconnecting;
- a beat every 30 s while connected;
- a failing check counting as disconnected;
- an unwritable heartbeat being a warning without the path;
- `jotflow health` fresh, stale, missing, and garbled, plus `--max-age`, with no tokens and an `.env` present;
- `run_until_stopped` beating while connected and removing the heartbeat on a clean stop;
- a connection with no state not being watched.

Locally on the built image, with a read-only root and a `/tmp` tmpfs: no heartbeat exits 1, and a fresh heartbeat with `--network none` exits 0. CI runs the same, plus a check that Compose's healthcheck is `jotflow health`.

## Handoff notes

Worktree `~/jotflow-work/wt-security-health`, branch `feat/health-signal`.
