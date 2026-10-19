# Per-run isolation inside an agent service

## Links and status

- Issue: #181. It has no milestone, and it's the first security item after v0.1.0.
- Also serves: #54 (the agent containers) and #172 (Codex, a preset with built-ins we can't remove)
- Status: planning only. Nothing is built until the lead schedules it.
- Last verified: written against main, 2026-10-19

## The gap

Every run in one agent service shares uid 10002, and so does the runner. Linux lets a same-uid process read another's `/proc/<pid>/environ` and its files, whatever Yama's `ptrace_scope=1` says. So a run that has any file or shell ability can read four things:

- the runner's environment, which holds that kind's GitHub token and the runner secret
- with the runner secret, it can also start more runs of its own kind
- concurrent runs' 0600 MCP configs, and so their run tokens
- the service's shared home: the CLI's login, caches, and settings

The other kind's token and runner stay out of reach. That boundary is the split into two containers and two networks, and this work keeps it.

Today this is accepted, because the claude-code preset has no file or shell tools, Codex refuses execution, and a custom command is the team's responsibility. But one CLI release that adds a tool, or one preset mistake, turns it into a token leak.

## Objective and done-when

From the issue, refined:

- [ ] **A test in the agent image,** in the cross-container CI job, shows that a run can't read:
  - another live run's `/proc/<pid>/environ`, run directory, or MCP config
  - the runner's `/proc/<pid>/environ`, or the runner secret and GitHub token by any path a run can reach
- [ ] **A run can't start another run.** It never sees the runner secret.
- [ ] **Model logins still work.** The API key in the env works. A CLI login kept in the home works too, or is clearly documented as unsupported.
- [ ] **The container stays hardened:** a read-only root, `no-new-privileges`, and no capability beyond what this design needs, with that one named and justified.
- [ ] **docs/threat-model.md** narrows or removes the "Runs of the same kind can read each other's environment" row.

## Options

| Option | How | Gains | Costs |
|---|---|---|---|
| **A. A uid per run, from a pool** | The image creates, say, eight unprivileged uids, 20001 to 20008. The runner keeps `CAP_SETUID` and `CAP_SETGID`, and nothing else. For each run it chowns the 0700 run directory to a free uid, then starts the CLI under that uid with `setresuid`/`setresgid` in `preexec_fn`, so the child has no capabilities. | The runner's environ and other runs' files and environs are unreadable, because the uid differs. No new binaries. It works under Docker's default seccomp, and `no-new-privileges` stays on, since dropping uids isn't exec'ing a setuid file. | The runner holds two capabilities, so a bug in the runner itself is worth more. The shared home needs care. The pool caps concurrency, which is fine at `MAX_LIVE_RUNS` = 8. |
| **B. A sandbox per run** (bubblewrap or nsjail) | Each run gets its own user, PID, and mount namespaces: a private `/proc`, and only the run directory, the CLI's install, and a per-run home mounted. | The strongest isolation, which also hides the process list and the filesystem. | Docker's default seccomp blocks creating user namespaces for unprivileged processes, so it needs a custom seccomp profile or `--privileged`-like settings, which weakens the container. It also adds a binary and its updates. |
| **C. Secrets out of the runner's environment** | The runner reads its secret and the GitHub token from 0400 files, and Compose passes them as files, not `env_file`. With A, the files belong to the runner's uid, so runs can't read them. | It shrinks `/proc/1/environ` to nothing secret. | On its own it does nothing, because a same-uid run can read the files too. It's only worth doing together with A. |

## Recommendation (revised with security's input, 2026-10-19)

The direction is option A. Option C is dropped: with per-run uids, the runner's `/proc/<pid>/environ` is already unreadable to runs, because that needs the same uid or `CAP_SYS_PTRACE`. And `env_file` isn't a file inside the container. Compose's file secrets are bind mounts whose uid, gid, and mode options aren't honoured outside swarm, so C would only add a host-side step.

**Security's corrections to A:**
- **Where the capability comes from.** `cap_add: [SETUID, SETGID]` probably never reaches a runner that starts as uid 10002 under `no-new-privileges`. Docker gives a non-root user no ambient capabilities, and `no-new-privileges` blocks file capabilities. The working variant is a runner that starts as **uid 0, with `cap_drop: ALL` plus only SETUID and SETGID**. It has no DAC_OVERRIDE, so it can't read others' 0700 files. When it drops a child to a pool uid, the kernel clears every capability, by the uid 0 to non-zero rule. Python's `subprocess` already has `user=`, `group=`, and `extra_groups=[]`, so there's no hand-written preexec.
- **No chown.** Changing ownership would need CAP_CHOWN. Use a group per run instead. The runner is in every run group, `runN`. A run directory is `0770 runner:runN`, and its config is `0640 runner:runN`. The run's uid is in `runN` only. A runner may chgrp a file to its own groups without any capability.
- **Blast radius.** A runner bug with SETUID can become any uid, including 0, and read every run's files. That runner already holds the token and secret, so the marginal loss is small. The rule is that the runner's code stays tiny, and nothing but `exec` happens after the drop.

**Sweeping a uid before reuse:**
- **Processes.** A reaper child drops to the run's uid and loops `kill(-1, SIGKILL)` until ESRCH. That kills everything the uid owns, including anything that daemonized, without CAP_KILL.
- **Files.** Mount `/tmp` as the runner's own 0711 directory, with run directories below it. Otherwise, sweep files the uid owns on `/tmp` and `/dev/shm`. SysV IPC and POSIX queues are per container, so either `ipcrm` by uid or accept that risk.
- **Rotation.** Round-robin over a pool of 64, so a uid is rarely reused soon after it's freed. A uid whose sweep found anything isn't handed out again until it's clean.

**Not closed by uids:** the network namespace is shared. A run can listen on loopback, and a concurrent run can connect to it. That's a cross-run channel, not a token leak, and the runner's own port stays guarded by its secret. It goes in the threat model as accepted.

**Logins are API keys only in this mode.** A copied CLI login can't be read-only: Claude Code refreshes OAuth tokens and rewrites its credentials file, so a per-run copy goes stale and can break the next run. Every run would also get the long-lived credential. CLI logins are documented as unsupported with per-run uids. A later credential broker could change that.

**Rootless Docker and Podman** map container uids into the host's subuid range, 65536 by default, so the pool fits, and capabilities are namespaced the same way inside. CI covers rootful Docker only, and the docs say so. **It fails closed:** at startup, and for each run, the runner checks that the child's uid is the pool uid and that its CapEff is 0, and otherwise refuses.

**The spike, before any build:**
1. Is CapEff non-zero in the runner's `/proc/self/status` under the uid 0 plus SETUID and SETGID variant? And under the uid 10002 variant, to confirm security's point?
2. Inside a dropped run, are CapEff, CapPrm, and CapAmb all 0, and is NoNewPrivs 1?
3. The four reads from the gap, from one run against another and against the runner: all must fail.
4. Does the reaper's `kill(-1)` clean up a daemonized child?

Security has offered to review the spike's results.

## Progress checklist

- [ ] Agree the approach with security and the lead
- [ ] The spike above, with its results reviewed by security
- [ ] The image and Compose changes: a pool of 64 uids and groups, a runner started as uid 0 with only SETUID and SETGID, and `/tmp` as the runner's 0711 directory
- [ ] The runner: the uid and group pool, group-owned run directories, `subprocess` `user=` and `group=`, the reaper sweep, and failing closed on a drop that didn't take
- [ ] The cross-container CI test: two concurrent stand-in runs try to read each other's environ, run directory, and config, and the runner's environ and secret files, and fail
- [ ] Docs: threat-model.md, architecture.md, docs/agent-adapters.md, and docs/deployment.md
- [ ] `make check` and the agent-image CI pass

## Validation

None yet. This is a plan only.

## Handoff notes

Nothing is built. Start with the spike in the recommendation. Security's input of 2026-10-19 is folded in above.
