# Setup and check catch a login the runner never passes

## Links and status

- Issue: #239
- PR: #240
- Status: ready for review
- Last verified: `make check`, 2026-10-20
- Next safe action: review

## Objective and done-when

The v0.1.0 clean-install test kept pre-filled agent env files whose model key wasn't in `JOTFLOW_AGENT_ENV_ALLOW`. Every run would have failed to sign in, and `jotflow check` said ok.

- [x] Setup adds the preset's key to the allowlist in a kept file that sets the key but doesn't allow it, and says it did.
- [x] `jotflow check` fails on such a file, naming the file and the line to set.
- [x] The "Agent model" prompt shows its default in brackets.
- [x] Tests cover each case.

**Out of scope:** a file that sets no key at all. claude-code can log in inside its container instead, so that's not an error.

## Design

**One rule, used by both.** `agent_setup.LOGIN_KEYS` maps each preset to the keys it can read: `ANTHROPIC_API_KEY` for claude-code, and `CODEX_API_KEY` or `OPENAI_API_KEY` for codex. `unpassed_logins(env, preset)` returns the keys a file sets that its allowlist doesn't pass. It returns nothing when one of the set keys is already passed, since codex needs only one. Each file is judged by the preset its runner must start, from `expected_runner_preset`, so under codex `agent-exec.env` is checked for claude-code's key.

**Setup** appends the missing names to the file's allowlist, keeping any names already there, and adds one line to its notes:

```
  agent.env sets ANTHROPIC_API_KEY but didn't pass it to the agent, so setup added it to JOTFLOW_AGENT_ENV_ALLOW.
```

The codex path already adds its own key, so this applies to claude-code files.

**Check** adds `agent_failures`, printed as `fail  agent:` lines, and exits with the new `EXIT_FAILED`, 1. The line names the file and gives the whole allowlist line to set, with no value shown. Blank-field reminders stay warnings and still exit 0. With `--live`, the live check still runs, and a failure there takes precedence over the agent's.

**The model prompt** passes `current or "default"` as its default. With no model set it shows `[default]`, which the explanation above it defines. With one set, it shows that model. Pressing Enter behaves as before.

## Validation

- `make check` passes.
- Re-capturing setup's transcript shows `Agent model [default]:`. docs/install.md's sample is updated, and a new paragraph there explains `fail` lines.

## Handoff notes
