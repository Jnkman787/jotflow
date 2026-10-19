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

## Recommendation

Do **A plus C**. A gives per-run separation without loosening the container's seccomp or `no-new-privileges`. C makes sure the runner's own secrets are files only its uid reads. B stays the option for a later, stronger step, if a preset ever needs a shell.

**Sketch of A plus C:**

- **The image.** Create the run uids 20001 to 20008 in one group, `agent-runs`. The runner stays 10002, and its environment becomes empty of secrets.
- **Compose.** Use `cap_add: [SETUID, SETGID]` on both agent services, and keep everything else dropped. Mount the runner secret and the GitHub token as Docker secrets (files), owned by 10002 with mode 0400, instead of `env_file` entries.
- **The runner.**
  - It reads its secret and token files at start.
  - For each run, it takes a free uid, makes the run directory owned by that uid and mode 0700, and writes the MCP config as that uid. The config is 0600, so other runs can't read it.
  - It starts the CLI with `setresgid`, `setgroups([])`, and `setresuid` to the run uid, and the env allowlist as now.
  - When the run ends, it kills the uid's processes (the process group, plus a sweep by uid) before reusing the uid.
- **The home.** Each run gets its own home inside the run directory. A CLI login kept in the service's home volume is copied in, read-only, per run, or the docs say API keys only per preset. Claude Code's `~/.claude.json` counters are then per run, which is fine.
- **The GitHub MCP server** is started by the CLI, so it runs as the run uid and reads the token from the 0600 config that uid owns. That's unchanged.

## Risks and open questions

- **`CAP_SETUID` in the runner:** what a runner bug could do with it, against what it buys. The runner is small, and its HTTP surface is gated by the secret, the Host allowlist, and a size cap.
- **Reusing a uid** must never hand a new run something an old run left behind, such as processes, files in `/tmp`, or System V IPC. The plan is a uid sweep plus a fresh run directory, and if possible `/tmp` per run (`TMPDIR` is already the run directory).
- **CLI logins.** Should a copied-in login be read-only? Some CLIs refresh tokens and rewrite their login file. This needs checking per preset, which favours API keys.
- **Rootless Docker and Podman:** capability semantics differ, so the plan should check both.
- **The cost to every preset.** The adapter guide (docs/agent-adapters.md) would add "the run executes as a fresh uid with a private home" to what the runner guarantees.

## Progress checklist

- [ ] Agree the approach with security and the lead
- [ ] The image and Compose changes (run uids, `cap_add`, secrets as files)
- [ ] The runner: the uid pool, ownership, `preexec` drop, the end-of-run sweep, and reading secrets from files
- [ ] The cross-container CI test: two concurrent stand-in runs try to read each other's environ, run directory, and config, and the runner's environ and secret files, and fail
- [ ] Docs: threat-model.md, architecture.md, docs/agent-adapters.md, and docs/deployment.md
- [ ] `make check` and the agent-image CI pass

## Validation

None yet. This is a plan only.

## Handoff notes

Nothing is built. Start with a spike: a runner child that drops to a pool uid under the current Compose hardening, plus `cap_add: [SETUID, SETGID]`, and a probe that tries the four reads above.
