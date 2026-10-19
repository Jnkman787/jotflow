# Codex on equal terms with claude-code

## Links and status

- Issue: #41
- Also serves: #172, which added the codex preset
- Draft PR: to be opened with this plan
- Status: implementing
- Last verified: origin/main 2ef2e25, 2026-10-19
- Next safe action: build the setup changes in `commands/init.py`, then the image pin

## Objective and done-when

The maintainer decided on 2026-10-19 that codex gets equal terms with claude-code. Each team picks its agent, and JotFlow doesn't steer the choice. Codex's two safety limits stay as they are until the maintainer decides otherwise. It's refused for execution runs, and its model is limited to `gpt-5.5`.

Done when:

- [ ] Setup's step 6 offers `claude-code` and `codex` side by side, in the same words, with no preferred agent. `none` stays the default when nothing is typed.
- [ ] Choosing `codex` writes `agent.env` with `JOTFLOW_AGENT_PRESET=codex`. Setup asks for the OpenAI API key, hidden, or explains how to add it later. The key goes in as `CODEX_API_KEY`, listed in `JOTFLOW_AGENT_ENV_ALLOW`.
- [ ] `agent-exec.env` stays on `claude-code` and its Anthropic login, and setup says so in one plain line.
- [ ] The published agent image carries both CLIs. Both compose files pin `AGENT_NPM_PACKAGES="@anthropic-ai/claude-code@2.1.303 @openai/codex@0.163.0"`, which release.yml reads. #205's npm purge and its guard still pass on a build with both. A test pins each CLI's version to its preset's constant.
- [ ] The "experimental" label is gone from the runner's startup line, presets.md, the README's "Other agents and models", jotflow.example.toml, agent.env.example, the CHANGELOG, docs/releases/v0.1.0.md, the roadmap, and the threat model. presets.md says what's verified, which is the CI tool-set gate on 0.163.0, and that no real-model pass is recorded yet. The two safety limits are documented plainly, as limits.
- [ ] The README sample session is re-captured from a real run, if setup's prompts changed.
- [ ] The image-size change is measured and reported.

**Out of scope:**
- Either safety limit. Lifting the execution refusal or the model pin waits for the maintainer.
- A model question for codex. With one model allowed, there's nothing to choose.
- Checking the OpenAI key against OpenAI. That would be an outward call during setup. The format check catches a pasted Anthropic key or a stray token.

## Design

**Setup, step 6.** `SUPPORTED_PRESETS` becomes `claude-code`, `codex` and `command`. The explanation names both agents in one sentence, in alphabetical order, with nothing marked as preferred. `none` stays the default.

**Choosing codex.**
- **agent.env:**
  - Gets `JOTFLOW_AGENT_PRESET=codex`. `JOTFLOW_AGENT_MODEL` is removed, since codex runs only `gpt-5.5` and the runner refuses any other model.
  - A per-kind model key that names another model is left in place, with a note to remove it. Setup keeps hand-set per-kind keys, as it already promises.
- **The login:**
  - If agent.env already passes a non-blank `CODEX_API_KEY` or `OPENAI_API_KEY`, setup keeps it and says so by name.
  - Otherwise setup asks `Paste the OpenAI API key for Codex (sk-, hidden), or press Enter to add it later`. A pasted key must start with `sk-` and must not start with `sk-ant-`, which is an Anthropic key. It's written as `CODEX_API_KEY`, which is added to the allowlist.
  - On Enter, setup writes a blank `CODEX_API_KEY=` and lists it, so the file has a slot to fill, and `jotflow check` flags it blank until it's filled.
- **The Anthropic key in agent.env:** codex doesn't use it.
  - When the value is blank, setup removes the line and its allowlist entry.
  - When it holds a value, setup leaves it and notes that agent.env still passes it to the agent. Setup never deletes a secret a person typed.
- **agent-exec.env:**
  - Its preset comes from `expected_runner_preset(EXECUTION_KINDS, "codex")`, which is `claude-code`. That's the same rule the orchestrator and `jotflow check` use.
  - The model question is skipped, so its model is left alone.
  - Setup says: `agent-exec.env stays on claude-code, since codex doesn't serve execution runs. It uses the Anthropic login, as its comments describe.`
- **Switching back from codex to claude-code:** agent.env goes back to `claude-code`. A kept `CODEX_API_KEY` in the allowlist gets the same "still passes it" note, so the OpenAI key isn't handed to Claude Code without saying so.

The pasted key is held in a `Secret`-like value whose `repr` hides it. It's written through `write_envs`, which already refuses multi-line values, with the same atomic, all-or-nothing write as the runner secrets.

**The image.** Both compose files pin both CLIs, and release.yml already reads the pin from `docker-compose.yml`. The Dockerfile expands the build argument unquoted, so the two packages install in one `npm install`. The purge and guard run after the install, as before.
- **Tests:** the existing claude-code pin test grows into one that checks `CLAUDE_CODE_VERSION` and `CODEX_VERSION` against both compose files.
- **Size:** measured locally, with both versions built from this branch's Dockerfile.

**The label.** `EXPERIMENTAL` becomes an empty frozenset, and the startup-line code stays for a future preset. docs/agent-adapters.md says the maintainer decides when a preset drops the label. Elsewhere the wording is factual: drafting and question runs only, `gpt-5.5` only, the CI tool-set gate on 0.163.0 verified, and no real-model pass recorded yet.

**Alternatives considered:**
- **Ask for a model under codex.** It lost, because only one model passes the runner.
- **Put the OpenAI key under `OPENAI_API_KEY`.** It lost, because `CODEX_API_KEY` names its one use, and another tool in the image won't pick it up by accident. A hand-set `OPENAI_API_KEY` is still accepted.
- **Ship a second image for codex.** It lost, because a uvx install pins one agent image per release, and setup can't pick an image.

## Risks and open questions

- **The image grows by the Codex package**, which carries a native binary per platform. The size is reported in the PR. The amd64 and arm64 release builds already run on every PR, without pushing.
- **The npm purge guard depends on Debian's npm.** It's checked on a local build with both packages.
- **Security reviews the image pin and the setup changes**, including how the OpenAI key is handled.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Setup: offer codex, write agent.env and agent-exec.env, ask for the key
- [ ] Image: pin both CLIs, add the version test, measure the size
- [ ] Drop the label, in code and docs
- [ ] Re-capture the README sample session
- [ ] Add or update tests
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

## Handoff notes
