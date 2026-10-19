# Setup readiness: four fixes from the quickstart pass

## Links and status

- Issue: #41 (the release's fresh-eyes review). These are the code findings 5 to 8 from the quickstart pass of 2026-10-19. The README findings went to product.
- Status: ready for review

## Objective and done-when

- [x] **5. `jotflow check` reports the agent's readiness.** With `[agent] preset` set, it warns in these cases:
  - agent.env or agent-exec.env is missing
  - a required key in either file is blank: the runner secret, the GitHub token for that file, the preset or command, and every model-login name `JOTFLOW_AGENT_ENV_ALLOW` lists
  - the two files name a different preset from jotflow.toml
  - `.env` lacks `JOTFLOW_RUNNER_SECRET` or `JOTFLOW_RUNNER_EXEC_SECRET`
  - `COMPOSE_PROFILES` in `.env` lacks `agent`

  It never prints a value. Warnings leave the exit code at 0, because the configuration itself is valid.
- [x] **6. A rerun that keeps jotflow.toml still asks the preset question,** with the current preset as the default. If the answer changes the preset, setup rewrites jotflow.toml with only that changed, keeps the old file as .bak, and sets up the agent files as on a first run.
- [x] **7. Setup's closing "Next:" lists the three steps in order:** invite the bot, `jotflow check`, then `docker compose up -d`.
- [x] **8. The runner names a missing token before the split-token rule.** A blank `JOTFLOW_GITHUB_READ_TOKEN` on a drafting runner gives "JOTFLOW_GITHUB_READ_TOKEN is missing in agent.env". A blank write token on an execution runner gives "JOTFLOW_GITHUB_WRITE_TOKEN is missing in agent-exec.env". Holding the other kind's token is still refused as before.

Finding 9, an offline dry run of setup, is out of scope.

## Design

- **`check.py`** gets an `agent_readiness(policy, folder)` function returning warning lines, built on `envfile.read_env`. It warns on blank values and never reads one out loud. It reads the files next to `.env` in the working folder, which is where the README runs `jotflow check`.
- **`init.py`** gets the preset question split out as `_ask_preset`. `_ask_policy` and the keep path in `_policy_step` both use it. A kept policy with a changed preset becomes `existing` with only `agent.preset` updated, so `_save_policy` writes it with the usual .bak.
- **`runner.py`'s `RunnerConfig`** checks for a missing token per kind before the split rule.

## Progress checklist

- [x] Code and tests for 5 to 8
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

`make check` is green. The tests are these:

- **`jotflow check`, in tests/test_check_command.py:**
  - nothing is reported without a preset
  - a ready setup says so
  - a blank secret, profile, read or write token, or model key is each named, and no value ever appears in the output
  - a missing file and a preset mismatch are named
  - codex teams aren't warned for claude-code on execution
  - `command` needs `JOTFLOW_AGENT_COMMAND`
  - without `.env`, it says where to run the check
- **The rerun, in tests/test_init.py.** A kept jotflow.toml still asks the preset question. Turning claude-code on changes only `agent.preset`, keeps a .bak, and writes both agent files. The same preset leaves the file untouched. The closing lists the three steps.
- **The runner, in tests/test_runner.py.** A blank read or write token is named for its file, and holding the other kind's token is still refused.

**Wording that product's README fixes can quote:**
- The keep prompt is now "Keep its channels and repos?". Before it, setup says "Keep it to change only the agent preset next. Answer n to go through the channels again; each one's repos can be kept."
- The closing is "Next:", then the steps "1. Invite @bot…", "2. Check the setup with: jotflow check", and "3. Start JotFlow with: docker compose up -d".
- Check's new lines are "ok    agent: <preset>, both agent files filled in" or "warn  agent: …".
