# `jotflow init` writes the runner secrets and the agent preset

## Links and status

- Issue: #126
- Also serves: #137 (#22's presets: init names the preset and model in the agent env files), D6, #54
- Draft PR: #143
- Status: ready for review
- Last verified: 2026-10-18, `make check` passes with the code below
- Next safe action: review, including security's second look

## Objective and done-when

`jotflow init` sets up the agent containers' side of `.env`, so nobody generates runner secrets by hand. It writes two distinct secrets. One goes in `agent.env` and in `.env` as `JOTFLOW_RUNNER_SECRET`. The other goes in `agent-exec.env` as `JOTFLOW_RUNNER_SECRET` and in `.env` as `JOTFLOW_RUNNER_EXEC_SECRET`. It also names #137's preset, and optionally a model, in both agent env files.

Done when:

- [x] Init generates each secret with `secrets.token_urlsafe(32)`. It writes `.env`, `agent.env`, and `agent-exec.env` atomically with mode 0600, and a test checks that the two secrets differ
- [x] A re-run keeps both secrets and keeps each pair of files in step. A rotation replaces either secret alone, or both
- [x] When the agent is set up, `.env` gains `COMPOSE_PROFILES=agent`, and any other profiles already there are kept
- [x] No secret is printed or logged. Nothing from `.env` except the runner secrets reaches the agent env files
- [x] Both agent env files get `JOTFLOW_AGENT_PRESET`, plus `JOTFLOW_AGENT_MODEL` when the person chooses one. A file that sets the `JOTFLOW_AGENT_COMMAND` escape hatch gets neither
- [x] Tests cover the first run, a re-run, a rotation of each secret, mismatched files getting a fresh secret, an interrupted rotation that never reverts, a failed write that changes no file, and the `command` preset across a re-run
- [x] README setup and the agent container section of Deployment say init does this
- [x] `make check` passes, including the coverage floor

**Out of scope:**
- Asking for the GitHub tokens or the model login. They stay as blank lines that the file's comments explain. A later issue can add them to init.
- Removing `agent.preset` from `jotflow.toml`. That is the architect's call; see Risks.

## Design

- **One preset question, as today, in step 6.** Its answer, or the preset in a `jotflow.toml` the person keeps, drives the agent env files as well as `agent.preset`. So there is one question and the two places never disagree. A new step would sometimes be skipped, which makes "step N of 7" misleading, and moving the question would only churn the policy step.
  - When a preset is chosen, setup next asks for the model, optionally. Enter keeps the value already in the files, or the preset's defaults if there is none. Typing `default` removes the setting. The answer goes into both files as `JOTFLOW_AGENT_MODEL`, checked against #137's pattern. Per-kind overrides are never written, and existing ones are kept. The prompt says how to set them by hand.
  - `none` leaves both agent files and the secrets untouched, and adds no profile. Asking to rotate then says there is nothing to rotate.
  - An invalid `jotflow.toml` that the person keeps counts as no preset.
  - Nothing is written until the last question is answered, as before.
- **Secrets, in a pure function.** `agent_setup.choose_secrets(env, agent, agent_exec, rotate_drafting=, rotate_execution=)` takes the three files' current values and returns the values to write, with reasons to show:
  - A value is kept when it is at least 32 characters, which the runner requires.
  - When the two files of a pair hold different usable values, init makes a fresh secret for both and says to restart. Neither side wins, because either may hold a rotation made by hand after a leak, or the written half of an interrupted rotation (security's second look, adopted by the lead). When only one file has a value, it is copied to the other.
  - A missing or short value, or a rotation, generates a new one.
  - If the drafting and execution values come out equal, which can only happen by hand-editing, the execution one is regenerated, because JotFlow refuses to start with equal secrets. That loop is bounded and raises after five tries.
- **Rotation**: `--rotate-runner-secret`, `--rotate-exec-secret`, or both. A rotation reminds the person to restart with `docker compose up -d`, so both sides pick up the new value.
- **Writing.** `envfile.write_envs` writes `.env` and both agent files as one step. Every new file is staged as a fsynced 0600 temporary file first, so bad input changes nothing. If putting one in place fails, the ones already replaced get their old text back, and a new one is removed. A crash between two renames can still leave a mismatch, and the rule above then makes a fresh secret instead of reverting. A `None` value deletes a key, for `default`. Messages are printed only after the write succeeds.
- **New agent files** start from `agent.env.example` or `agent-exec.env.example` next to `.env` when one is there, so the comments come along. Otherwise they start empty. Only JotFlow's keys are set. Nothing is copied from `.env`, and a test proves it with sentinel tokens.
- **The `command` preset.** #142 makes JotFlow require `[agent] preset = "command"` when a runner uses `JOTFLOW_AGENT_COMMAND`, so init accepts `command` and keeps it on a re-run. It is the default when an agent file already sets a command. With it, init writes each file's secret, removes a stray `JOTFLOW_AGENT_PRESET`, skips the model question, and says when a file lacks the command. With `claude-code`, a file that sets a command gets only its secret, and init warns that JotFlow will refuse that runner.
- **`COMPOSE_PROFILES`.** It is added, or merged into an existing comma-separated value, when the preset is not `none` and each agent file holds its GitHub token. Otherwise init names the missing lines and says to re-run init or add the profile once they are filled in. Starting a runner without its token would only crash-loop.
- **Output.** Init says which files it wrote and which secrets were kept, copied, generated, or rotated. It never shows a value.
- Files: `src/jotflow/commands/init.py`, `src/jotflow/envfile.py`, and `src/jotflow/agent_setup.py` for the pure logic. `Secret`'s repr leaves out the value. Tests go in `tests/test_init.py`, `tests/test_envfile.py`, and `tests/test_agent_setup.py`.

## Risks and open questions

- **The preset's home** (settled by the lead on 2026-10-18: keep `agent.preset` from the same answer, matching #142's GET /info agreement check). #137 names the preset in the agent env files and leaves `jotflow.toml`'s `agent.preset` alone. Its description says "unset disables agent runs", which #55 might read. Default: one question, written to both places, as above. It also keeps the default at `none` until runs are wired (#136).
- **The model question** (settled by the lead: one optional shared model; per-kind by hand). Default: one optional `JOTFLOW_AGENT_MODEL` for both files, as above.
- #137 has merged, and this branch is rebased onto it. The env keys are #137's names, and the tests pin them.
- Rotating while the containers run breaks runs until both restart. Init says so.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; two defaults pending the architect
- [x] Commit this plan and open the draft PR
- [x] `envfile`: remove a key
- [x] Secret reconciliation and rotation, as a pure function
- [x] Step 6's preset drives the agent files: model, escape hatch, profiles, and writing order
- [x] Rotation flags
- [x] Tests: first run, re-run, each rotation, mismatch, escape hatch, no leak, distinct values
- [x] Docs: README setup, Deployment's agent container section, the example env files' comments
- [x] CodeRabbit once: skipped, the free CLI allowance was rate-limited
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` passes: end-of-line check, ruff lint and format, mypy strict, and the tests with the coverage floor.
- `tests/test_init.py` drives whole setup sessions. It covers a first run with three 0600 files and two distinct secrets, and a re-run that keeps both. It covers rotating each secret alone and both together, repairing a mismatch and a missing file from `.env`, and setting, keeping, and removing a model. It also covers a bad model name, the escape hatch, new files from the examples, the profile waiting for the GitHub tokens and merging with others, no preset, and the rotation flags. Every session checks that no secret appears in the output or the prompts.
- A sentinel test shows nothing from `.env` reaches either agent file: each holds only its secret and the preset.

## Handoff notes

None yet.
