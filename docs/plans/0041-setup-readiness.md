# Setup readiness: four fixes from the quickstart pass

## Links and status

- Issue: #41 (the release's fresh-eyes review). These are the code findings 5 to 8 from the quickstart pass of 2026-10-19. The README findings went to product.
- Status: implementing

## Objective and done-when

- [ ] **5. `jotflow check` reports the agent's readiness.** With `[agent] preset` set, it warns in these cases:
  - agent.env or agent-exec.env is missing
  - a required key in either file is blank: the runner secret, the GitHub token for that file, the preset or command, and every model-login name `JOTFLOW_AGENT_ENV_ALLOW` lists
  - the two files name a different preset from jotflow.toml
  - `.env` lacks `JOTFLOW_RUNNER_SECRET` or `JOTFLOW_RUNNER_EXEC_SECRET`
  - `COMPOSE_PROFILES` in `.env` lacks `agent`

  It never prints a value. Warnings leave the exit code at 0, because the configuration itself is valid.
- [ ] **6. A rerun that keeps jotflow.toml still asks the preset question,** with the current preset as the default. If the answer changes the preset, setup rewrites jotflow.toml with only that changed, keeps the old file as .bak, and sets up the agent files as on a first run.
- [ ] **7. Setup's closing "Next:" lists the three steps in order:** invite the bot, `jotflow check`, then `docker compose up -d`.
- [ ] **8. The runner names a missing token before the split-token rule.** A blank `JOTFLOW_GITHUB_READ_TOKEN` on a drafting runner gives "JOTFLOW_GITHUB_READ_TOKEN is missing in agent.env". A blank write token on an execution runner gives "JOTFLOW_GITHUB_WRITE_TOKEN is missing in agent-exec.env". Holding the other kind's token is still refused as before.

Finding 9, an offline dry run of setup, is out of scope.

## Design

- **`check.py`** gets an `agent_readiness(policy, folder)` function returning warning lines, built on `envfile.read_env`. It warns on blank values and never reads one out loud. It reads the files next to `.env` in the working folder, which is where the README runs `jotflow check`.
- **`init.py`** gets the preset question split out as `_ask_preset`. `_ask_policy` and the keep path in `_policy_step` both use it. A kept policy with a changed preset becomes `existing` with only `agent.preset` updated, so `_save_policy` writes it with the usual .bak.
- **`runner.py`'s `RunnerConfig`** checks for a missing token per kind before the split rule.

## Progress checklist

- [ ] Code and tests for 5 to 8
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

None yet.
