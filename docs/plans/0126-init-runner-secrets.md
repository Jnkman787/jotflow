# `jotflow init` writes the runner secrets and the agent preset

## Links and status

- Issue: #126
- Also serves: #137 (#22's presets: init names the preset and model in the agent env files), D6, #54
- Draft PR: to be opened
- Status: planning
- Last verified: plan written on 2026-10-18, no code yet
- Next safe action: the architect confirms the two defaults under Risks; then extend `src/jotflow/envfile.py`

## Objective and done-when

`jotflow init` sets up the agent containers' side of `.env`, so nobody generates runner secrets by hand. It writes two distinct secrets. One goes in `agent.env` and in `.env` as `JOTFLOW_RUNNER_SECRET`. The other goes in `agent-exec.env` as `JOTFLOW_RUNNER_SECRET` and in `.env` as `JOTFLOW_RUNNER_EXEC_SECRET`. It also names #137's preset, and optionally a model, in both agent env files.

Done when:

- [ ] Init generates each secret with `secrets.token_urlsafe(32)`. It writes `.env`, `agent.env`, and `agent-exec.env` atomically with mode 0600, and a test checks that the two secrets differ
- [ ] A re-run keeps both secrets and keeps each pair of files in step. A rotation replaces either secret alone, or both
- [ ] When the agent is set up, `.env` gains `COMPOSE_PROFILES=agent`, and any other profiles already there are kept
- [ ] No secret is printed or logged. Nothing from `.env` except the runner secrets reaches the agent env files
- [ ] Both agent env files get `JOTFLOW_AGENT_PRESET`, plus `JOTFLOW_AGENT_MODEL` when the person chooses one. A file that sets the `JOTFLOW_AGENT_COMMAND` escape hatch gets neither
- [ ] Tests cover the first run, a re-run, a rotation of each secret, mismatched files, and the escape hatch
- [ ] README setup and the agent container section of Deployment say init does this
- [ ] `make check` passes, including the coverage floor

**Out of scope:**
- Asking for the GitHub tokens or the model login. They stay as blank lines that the file's comments explain. A later issue can add them to init.
- Removing `agent.preset` from `jotflow.toml`. That is the architect's call; see Risks.

## Design

- **A new step 7, "The agent containers".** It runs after the policy step, and like everything else it writes nothing until the last question is answered.
  - It asks for the preset: `claude-code`, or `none` to leave the agent off. The default is the preset in `agent.env`, then the one in `jotflow.toml`, then `claude-code`. Step 6's preset question moves here, so there is one question. When init writes `jotflow.toml`, it sets `agent.preset` to the same answer. When the toml is kept and names a different preset, init says so.
  - It asks for the model, optionally: Enter keeps the value already in the files, or the preset's defaults if there is none. Typing `default` removes the setting. The answer goes into both files as `JOTFLOW_AGENT_MODEL`. Per-kind overrides are never written, and existing ones are kept.
  - `none` leaves both agent files and the secrets untouched, and adds no profile.
- **Secrets, in a pure function.** `runner_secrets(env, agent, agent_exec, rotate) -> (drafting, execution)` takes the three files' current values and returns the values to write, with reasons to show:
  - A value is kept when it is at least 32 characters, which the runner requires.
  - When the two files of a pair disagree, the `.env` value wins and init says so, without showing either value. When only one file has a value, it is copied to the other.
  - A missing or short value, or a rotation, generates a new one.
  - If the drafting and execution values come out equal, which can only happen by hand-editing, the execution one is regenerated, because JotFlow refuses to start with equal secrets.
- **Rotation**: `--rotate-runner-secret`, `--rotate-exec-secret`, or both. A rotation reminds the person to restart with `docker compose up -d`, so both sides pick up the new value.
- **Writing.** `envfile.write_env` already writes atomically at 0600 and keeps comments and other keys. It gains removal: a `None` value deletes the key, for `default`. The agent files are written first and `.env` last. If init stops in between, the next run finds the mismatch and repairs it with the rule above.
- **New agent files** start from `agent.env.example` or `agent-exec.env.example` next to `.env` when one is there, so the comments come along. Otherwise they start empty. Only JotFlow's keys are set. Nothing is copied from `.env`, and a test proves it with sentinel tokens.
- **The escape hatch.** If a file sets `JOTFLOW_AGENT_COMMAND`, init still writes its secret but no preset or model, and says why: #137 makes both together a startup error.
- **`COMPOSE_PROFILES`.** It is added, or merged into an existing comma-separated value, when the preset is not `none` and each agent file holds its GitHub token. Otherwise init names the missing lines and says to re-run init or add the profile once they are filled in. Starting a runner without its token would only crash-loop.
- **Output.** Init says which files it wrote and which secrets were kept, copied, generated, or rotated. It never shows a value.
- Files: `src/jotflow/commands/init.py`, `src/jotflow/envfile.py`, and probably a small `src/jotflow/agent_setup.py` for the pure logic. Tests go in `tests/test_init.py` and `tests/test_envfile.py`.

## Risks and open questions

- **The preset's home** (asked of the architect). #137 names the preset in the agent env files and leaves `jotflow.toml`'s `agent.preset` alone. Its description says "unset disables agent runs", which #55 might read. Default: one question, written to both places, as above.
- **The model question** (asked of the architect). Default: one optional `JOTFLOW_AGENT_MODEL` for both files, as above.
- #137 is still open. Until it merges, `JOTFLOW_AGENT_PRESET` isn't read by main's runner, so this PR should merge after #137. The env keys are #137's names, and the tests pin them.
- Rotating while the containers run breaks runs until both restart. Init says so.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; two defaults pending the architect
- [ ] Commit this plan and open the draft PR
- [ ] `envfile`: remove a key
- [ ] Secret reconciliation and rotation, as a pure function
- [ ] Step 7: preset, model, escape hatch, profiles, and writing order
- [ ] Rotation flags
- [ ] Tests: first run, re-run, each rotation, mismatch, escape hatch, no leak, distinct values
- [ ] Docs: README setup, Deployment's agent container section, the example env files' comments
- [ ] CodeRabbit once, or skipped
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
