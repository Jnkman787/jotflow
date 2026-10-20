# Per-run Linux uids inside an agent service

## Links and status

- Issue: #181, in M6
- Also serves: #54 (the agent containers) and #172 (Codex, a preset whose built-ins we can't remove)
- Builds on: the architect's earlier draft in #200, closed without merging, with security's input folded in
- Status: planning. Decisions 1 to 4, 6 and 7 are approved by the lead. Decision 5 waits on the maintainer, and the plan follows the lead's opt-in shape for it. No code until the lead confirms.
- Last verified: a throwaway spike in the pinned `python:3.14-slim-trixie` base on 2026-10-20, under Compose's hardening: a read-only root, `cap_drop: ALL`, `no-new-privileges`, and Docker's default seccomp. Results are under Validation.

These are Linux user ids inside the agent containers, not GitHub or Slack accounts.

## The gap

Every run in one agent service runs as uid 10002, and so does that service's runner. Linux lets a same-uid process read another's `/proc/<pid>/environ` and its files, Yama's `ptrace_scope=1` notwithstanding. So a run with any file or shell ability can read:

- the runner's environment, which holds that kind's GitHub token and the runner secret, and with the secret, it can start more runs of its own kind;
- concurrent runs' 0600 MCP configs, and so their run tokens;
- the service's shared home: the CLI's login, caches and settings.

The other kind's token and runner stay out of reach, because of the split into two containers and two networks. This work keeps that split.

Today it's accepted. The claude-code preset has no file or shell tools, Codex refuses execution, and a custom command is the team's responsibility. But one CLI release that adds a tool, or one preset mistake, turns it into a token leak.

## Objective and done-when

From the issue, refined:

- [ ] **A test in the agent image,** in the cross-container CI job, shows that a run can't read:
  - another live run's `/proc/<pid>/environ`, run directory or MCP config;
  - the runner's `/proc/<pid>/environ`, or its secret or GitHub token by any path a run can reach.
- [ ] **A run can't start another run,** because it never sees the runner secret.
- [ ] **Isolation is opt-in for now:** `JOTFLOW_RUNNER_ISOLATION=uid` in an agent env file turns it on for that service. Off by default, today's single-uid behaviour is unchanged, so a CLI login kept in `agent-home` keeps working.
- [ ] **With isolation on, model logins work with an API key in the environment.** A CLI login kept in the home is refused at runner startup, with a reason naming the setting.
- [ ] **The container stays hardened:** a read-only root, `no-new-privileges`, and only the two capabilities this design needs, named and justified.
- [ ] **docs/threat-model.md** narrows the "Runs of the same kind can read each other's environment" row to what's left.

**Out of scope:** network separation between runs of one kind, and the drafting/execution split, which stays as it is.

## Options

| Option | How | Gains | Costs |
|---|---|---|---|
| **A. A uid per run, from a pool** | The runner starts as uid 0 with `cap_drop: ALL` plus only `SETUID` and `SETGID`. It starts each run's CLI as a free pool uid with `subprocess`'s `user=`, `group=` and `extra_groups=[]`. | Runs can't read each other's environment or files, or the runner's. No new binaries. It works under Docker's default seccomp, and `no-new-privileges` stays on. | The runner holds two capabilities, so a runner bug is worth more. Stopping a run needs care, because the runner can't signal another uid: see decision 3. The home and `/tmp` need care too. |
| **B. A sandbox per run** (bubblewrap or nsjail) | Each run gets its own user, PID and mount namespaces: a private `/proc`, with only the run directory, the CLI install and a per-run home mounted. | The strongest isolation, which also hides the process list and the filesystem. | Docker's default seccomp blocks unprivileged user namespaces, so it needs a custom seccomp profile or near-privileged settings, which weakens the container. It also adds a binary to keep up to date. |
| **C. Secrets out of the runner's environment** | The runner reads its secret and token from 0400 files. | Nothing secret shows in `/proc/1/environ`. | On its own, a same-uid run reads the files too. With A, it's redundant: a different uid can't read the runner's environment at all. Compose's file secrets also ignore `uid`, `gid` and `mode` outside swarm, which would need a host-side step. |

## Recommendation: A

### Decisions (for the lead to approve)

1. **The runner starts as uid 0, with `cap_drop: ALL` plus only `SETUID` and `SETGID`.** The spike shows a runner started as uid 10002 can't drop to another uid even with `cap_add`. That fails with EPERM, because Docker gives a non-root user no ambient capabilities and `no-new-privileges` blocks file capabilities. As uid 0 with only those two capabilities, the drop works, and the child ends with CapEff, CapPrm and CapAmb all zero and NoNewPrivs set. The runner has no DAC_OVERRIDE, CHOWN or KILL, so it can't read other uids' 0700 files.

2. **A pool of 64 uids and groups, 20001 to 20064, handed out round-robin.** A uid is never shared between live runs: the spike shows a same-uid process can still read the run's environment. A uid isn't reused until the sweep in decision 3 finds nothing left. `MAX_LIVE_RUNS` is 8, so the pool leaves plenty of slack.

3. **The runner stops a run through a helper running as that run's uid.** The spike's other finding: a uid-0 runner without CAP_KILL can't signal its own child once the child runs as a pool uid, and gets EPERM. So the runner's stop, timeout and sweep use one short helper started as the run's uid, which calls `kill(-1, SIGKILL)`. That kills everything the uid owns, a daemonized grandchild included, as the spike shows. The runner then reaps its own child, which ends the zombie. That keeps the capability set at two. The alternative is to add `KILL`, a third capability that would let the runner signal anything in the container.

4. **No chown: a group per run.** The runner is in every run group. A run's directory is `0770 root:runN` and its config `0640 root:runN`, and the run's uid is in `runN` only. As root, the runner may chgrp its own files to its own groups without CAP_CHOWN. The `/tmp` tmpfs becomes the runner's 0711 directory, with run directories below it, so a run can't leave files in a shared world-writable place. The sweep also clears `/dev/shm` of the uid's files before reuse.

5. **Opt-in first, with API keys required only when it's on** (the lead's proposed shape, pending the maintainer). A copied CLI login can't stay read-only, because CLIs such as Claude Code refresh OAuth tokens and rewrite their credentials file, and every run would get the long-lived credential. So per-run uids break teams that sign the CLI in with a subscription login. Therefore:
   - `JOTFLOW_RUNNER_ISOLATION` in each agent env file takes `none`, the default, or `uid`. The runner reads it at startup, and its startup line says which mode is on.
   - With `none`, nothing changes from today: one uid, the shared `agent-home`, and CLI logins work.
   - With `uid`, each run gets a per-run home inside its run directory, and the runner refuses to start if the preset's model login isn't an API key in the environment. That's `ANTHROPIC_API_KEY` for claude-code, and `CODEX_API_KEY` or `OPENAI_API_KEY` for codex. The reason names the setting and the key.
   - **How Compose allows the two modes** is open for the lead. Recommended is (b).
     - (a) The agent services always start the runner as uid 0 with only SETUID and SETGID. In `none` mode, the runner drops itself to 10002 for good at startup. One Compose file serves both modes, but the default deployment starts as root, which it doesn't today.
     - (b) The default Compose files stay exactly as today: uid 10002 and no capabilities. Turning `uid` on means adding a small override, `docker-compose.isolation.yml`, shipped next to the policy override. It adds `user: "0:0"` and `cap_add: [SETUID, SETGID]` to the agent services, and `COMPOSE_FILE` lists it. The runner fails closed if `uid` is set but it didn't start as uid 0 with exactly those two capabilities, and the reason names the override. The default footprint doesn't change at all.
   - `jotflow init` asks nothing new yet, and writes `none`. `jotflow check` reports the mode, and warns in `uid` mode if the login isn't an API key.
   - Making `uid` the default is left for a later minor release, and the CHANGELOG will say so ahead of time.

6. **Fail closed.** At startup, and for each run, the runner checks that the child's uid is the pool uid and that its CapEff is zero. If the drop didn't take, it refuses, as on an odd runtime. The runner's code after the drop is only `exec`.

7. **Rootless Docker and Podman** map container uids into the host's subuid range, 65536 by default, so 20001 to 20064 fit, and capabilities behave the same inside. CI covers rootful Docker only, and the docs say so. Decision 6 is the safety net.

### What stays open, and goes in the threat model as accepted

- **The network namespace is shared.** A run can listen on loopback and a concurrent run can connect to it. That's a channel between runs of one kind, not a token leak, and the runner's own port is still guarded by its secret.
- **A runner bug is worth more.** With SETUID, a runner compromise can become any uid in the container, root included. That runner already holds its kind's token and secret, so the extra loss is small. The rule is that the runner stays small, and nothing but `exec` runs after the drop.

## Design

- **Dockerfile.agent:** create groups and users `run20001` to `run20064`, with no home, no shell and no login, and make the runner a member of every run group. The image's `USER` becomes root for the runner only.
- **Compose, both files and the templates:**
  - Add `user: "0:0"`, `cap_add: [SETUID, SETGID]` and keep `cap_drop: ALL` on `agent` and `agent-exec`.
  - The `/tmp` tmpfs becomes `mode=0711`.
  - Nothing else changes: `read_only`, `no-new-privileges` and `dns_search` all stay.
- **adapters/runner.py:**
  - a `UidPool` handing out uids round-robin, skipping any still in use or not yet swept;
  - `_run` makes the group-owned run directory, writes the config `0640`, and starts the CLI with `user=`, `group=` and `extra_groups=[]`;
  - `_kill` and `_watch` stop the run through the same-uid helper, then reap;
  - the end-of-run sweep kills the uid's processes, removes its files under `/dev/shm`, and frees the uid;
  - the startup and per-run checks from decision 6.
- **Presets:** each run's `HOME` points at a directory inside its run dir. The runner already sets `HOME`, `TMPDIR` and the run directory per run, so that's a one-line change.
- **The CI cross-container job** starts two concurrent stand-in runs and checks:
  - each tries to read the other's environ, run directory and config, and the runner's environ, and fails;
  - each reports CapEff 0 and NoNewPrivs 1;
  - a daemonized child is gone after the run ends.
- **Docs:** threat-model.md (the row narrowed, and the two accepted items above), architecture.md, docs/agent-adapters.md (what the runner guarantees a preset), deployment.md (API keys only, and rootless notes) and the CHANGELOG.

## Risks

- **A CLI that refuses to run as a uid with no passwd home,** or that needs a writable home outside the run directory. The per-run `HOME` covers the known presets, and the stand-in test catches a regression.
- **Root in the container,** even with two capabilities, is a change from today's non-root user. That's why decision 6 checks the drop every time, and the runner stays as small as it is today.
- **A CLI login in `agent-home`** keeps working, because isolation is off by default. A team that turns `uid` on must switch to an API key first, and the runner refuses to start until it has.
- **With (a), `none` mode starts as uid 0 too.** The runner drops to 10002 for good before serving. With (b), `none` is today's container unchanged. Either way, the cross-container test covers both modes: in `none`, CapEff is 0 and runs share 10002, as today.

## Progress checklist

- [ ] The lead approves the decisions
- [ ] The image and Compose changes
- [ ] The runner: `JOTFLOW_RUNNER_ISOLATION` (`none` or `uid`), the startup drop in `none` mode, and in `uid` mode the pool, group-owned directories, the drop, the same-uid stop helper, the sweep, the API-key requirement, and the fail-closed checks
- [ ] Unit tests for the pool and sweep, and the cross-container test above
- [ ] Docs and the threat model
- [ ] `make check` and the agent-image CI pass

## Validation

The spike, on 2026-10-20, in the pinned base image with `--read-only --cap-drop ALL --security-opt no-new-privileges` and the default seccomp:

| Variant | Result |
|---|---|
| Runner as uid 10002, `--cap-add SETUID --cap-add SETGID` | Dropping a child to uid 20001 fails with EPERM. `cap_add` doesn't reach a non-root user. |
| Runner as uid 0, `--cap-add SETUID --cap-add SETGID` | The runner's CapEff is `0xc0`, only SETUID and SETGID. The child as 20001 has CapEff, CapPrm and CapAmb at 0 and NoNewPrivs 1. |
| Uid 20002 reads run A (uid 20001) and the runner's `/proc/<pid>/environ` | Both refused with EACCES |
| Uid 20001 reads its own run A's environ | Readable, which is why a uid is never shared between live runs |
| Runner `kill()`s its own child running as 20001 | EPERM without CAP_KILL, which is why decision 3 exists |
| A helper as uid 20001 calls `kill(-1, SIGKILL)` | Run A and its daemonized, `setsid` grandchild are both gone. The runner then reaps A, which exits -9. |
