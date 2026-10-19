# The agent adapter guide

## Links and status

- Issue: #40 (this is its third document; deployment.md came in #183 and threat-model.md in #178)
- Draft PR: #187
- Status: planning
- Last verified: plan written against main at 5e659f3, 2026-10-19
- Next safe action: commit this plan and open the draft PR

## Objective and done-when

#40 asks for "how to add an agent preset". The guide is `docs/agent-adapters.md`, written for someone adding support for another agent CLI, or running their own with `JOTFLOW_AGENT_COMMAND`. It explains what the runner already guarantees, what a preset must guarantee on top, and how to prove it. The proof uses the harness that exists today: the unit tests, `jotflow replay` with the stand-ins, the fake GitHub server, and the fake Responses API gate.

- [ ] `docs/agent-adapters.md` exists, and the README, presets.md, and threat-model.md link to it
- [ ] It covers the custom-command escape hatch and what the team takes on with it
- [ ] It covers the preset interface (`RunContext`, `RunPlan`, `files`, the registry, `EXPERIMENTAL`, `verified_models`, `check_install`), with a minimal example
- [ ] It states what a preset must guarantee: the exact tool set per run kind, with no shell or exec, layered limits, strict MCP loading, tokens only in private files, no run data in argv, the CLI's state kept in the run directory, failing closed on version, flag, and model, and transcripts off
- [ ] It says how to prove each guarantee, with a checklist a reviewer can tick
- [ ] The commands and names in it match the code. I'll check each against the source before marking ready.

**Out of scope:** new presets, such as Gemini and Copilot (an M5 issue), and changes to the runner or the harness. If writing the guide shows a gap, it goes in a new issue.

## Design

The guide has these sections:

1. **Who this is for, and the two ways in.** A named preset, which is supported and reviewed, or `JOTFLOW_AGENT_COMMAND`, which is the team's responsibility, as architecture.md says.
2. **What the runner already does for every run.** The 0700 run directory, the 0600 config, the env allowlist, the token split per container, the time limit and process-group kill, discarded output, and removal afterwards. This links to architecture.md rather than repeating it.
3. **Writing a preset.**
   - **The interface.** `RunContext` in, `RunPlan` out (`argv`, `config`, `env`, `github_token_env`, `files`), the `PRESETS` entry, and the optional `check_install` and `verified_models`.
   - **A minimal example** of a CLI that reads an MCP config file.
   - **Rules for the code:** a fixed per-kind prompt; MCP servers limited to `JOTFLOW_TOOLS` and `GITHUB_TOOLS` for the kind; the GitHub server `--read-only` for conversation runs; tokens only in private files; the CLI's home, cache, and logs inside the run directory.
4. **What a preset must guarantee,** as a table: each guarantee, why it matters (linked to threat-model.md), and how it's proven.
5. **Proving it.**
   - **Unit tests** that pin the argv and config per kind, and that no prompt carries run data (the `test_presets.py` patterns).
   - **The tool-set gate:** run the real CLI against `jotflow.testing.fake_responses_api`, or an equivalent fake for the CLI's provider, assert the exact offered tools, and script a write tool call that must be refused. `test_codex_gate.py` is the worked example. CI runs the gate with the CLI pinned by lockfile and `JOTFLOW_REQUIRE_…=1`, so it fails rather than skips.
   - **Free end-to-end checks:** `jotflow replay --agent scripted` and `--execute`.
   - **Real passes,** which need approval: `jotflow replay --agent <preset> [--execute] --yes`, recording counts only.
   - **The experimental label,** and when to drop it.
6. **A review checklist** for a new preset PR.
7. **Upgrading a pinned CLI:** re-run the gate and the real pass, then update docs/presets.md.

## Risks and open questions

- **Drift.** The guide names code. I'll keep it to stable names and link to the source and tests rather than copying code.
- **Overlap with presets.md.** presets.md stays the per-preset reference. The guide is how to write one.

## Progress checklist

- [x] Commit this plan and open the draft PR
- [x] Write `docs/agent-adapters.md`
- [x] Add links from the README, presets.md, and threat-model.md
- [x] Check every command and name against the code
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

I checked every function, field, constant, test name, environment flag, and CI job name the guide uses against the code on #172's branch. All exist. The guide relies on #172's `RunPlan.files`, `check_install`, `verified_models`, `fake_responses_api`, and the Codex gate, so **#187 merges after #172**.

## Handoff notes

None yet.
