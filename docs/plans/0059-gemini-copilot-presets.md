# Presets for Gemini CLI and Copilot CLI

## Links and status

- Issue: #59 (M6)
- Draft PR: #285
- Status: planning. The lead approved the plan on 2026-10-21 with the decisions below. Code waits for security
- Last verified: 2026-10-21, both CLIs in a throwaway build of `docker/Dockerfile.agent` from main, run with `--network none` against JotFlow's real MCP server and scripted fake model APIs
- Next safe action: review

## Objective and done-when

#59 asks for presets for two more agent CLIs, with the same per-run-kind tool allowlists and transcript settings as claude-code and codex. The lead asked for each CLI's pinned version and install, how it's locked to JotFlow's and GitHub's MCP tools, how unlisted tools are denied without prompting, how transcripts and memory stay off, its login and env allowlist, and which run kinds it can serve. All of it was to be checked against the real CLIs, and a CLI that can't be locked down is deferred.

**The finding, in short:**
- **Copilot CLI can be locked down,** for all three run kinds, with an API key from the team's own model provider. The plan builds it.
- **Gemini CLI can be locked down too, but it can't connect to JotFlow.** JotFlow serves only MCP 2026-07-28, and every Gemini release, including today's nightly, speaks at most 2025-11-25. This plan recommends deferring Gemini to a follow-up issue, with the verified recipe below, until Gemini's MCP client catches up.

Done when, from #59's boxes, for the `copilot` preset:

- [ ] **The preset** is in `PRESETS` and `EXPERIMENTAL`, pinned, with `check_install()`, and serves drafting, question and execution
- [ ] **A security gate in CI,** like codex's, runs the pinned Copilot with the preset's own command and config against a fake model, for each run kind. It shows the exact offered tool set, a refused shell call that writes nothing, an unlisted tool denied without a prompt, and that the CLI exits by itself
- [ ] **presets.md** documents its MCP config shape, its headless permission handling, its login, what's verified, and what isn't
- [ ] **A real pass** with `jotflow replay --agent copilot` is recorded, or presets.md says none is, and the preset stays experimental. A real pass costs model tokens and needs the maintainer's approval
- [ ] `make check` passes

**Out of scope:** a Gemini preset (#286), Copilot's own GitHub-account login (#287), Anthropic's API for copilot (#288), setup offering either CLI (a follow-up once the preset leaves experimental), and changes to JotFlow's MCP server.

## What was checked, and how

A throwaway image built from `docker/Dockerfile.agent` on main, with `AGENT_NPM_PACKAGES="@google/gemini-cli@0.65.0 @github/copilot@1.0.101"`. Each CLI ran as the `agent` user with `--network none`, so nothing could reach the internet. A small harness started JotFlow's real MCP server with a real run token, the fake GitHub MCP server, and a scripted fake model. The fake model recorded every request and answered with one scripted tool call, then "done". For Copilot that's the Responses API that the codex gate already uses. For Gemini it's a small Gemini API stand-in. The harness and its configs aren't committed: the gate below replaces them.

Both CLIs run on the image's Debian Node 20. Copilot's npm package is a launcher plus a native binary for each platform, which bundles its own Node runtime.

## Copilot CLI

**Pinned version:** `@github/copilot@1.0.101`, the latest stable release on 2026-10-21. It goes in `AGENT_NPM_PACKAGES`, and `check_install()` requires `copilot --version` to print exactly `GitHub Copilot CLI 1.0.101.`. Copilot releases almost daily, so the pin moves only through the "Upgrading a pinned CLI" steps in agent-adapters.md.

**Install size.** Debian's npm installs both the glibc and the musl native packages, about 188 MB each. The Dockerfile removes the musl one after install, and checks that `copilot --version` still works.

**The command,** for a run of each kind. It carries no run data, and the prompt is the shared fixed prompt for the kind:

```
copilot -p <the kind's fixed prompt>
  --additional-mcp-config @<run dir>/mcp.json
  --disable-builtin-mcps --no-custom-instructions --no-ask-user
  --no-auto-update --disable-client-telemetry --log-level none
  --available-tools <the kind's tools, as server-tool>
  --allow-tool jotflow(<tool>) ... --allow-tool github(<tool>) ...
```

**Locked to the kind's MCP tools.**
- `--available-tools` is the hard allowlist. With it, the model was offered exactly the kind's JotFlow and GitHub tools and nothing else, for drafting and execution. Without it, Copilot 1.0.101 offers 18 built-ins, including `bash`, `apply_patch`, `view`, `web_fetch`, `sql`, `task` and the agent tools.
- The set didn't change across the models tried: `gpt-5.5`, `gpt-4.1`, `claude-sonnet-5-5` and `gemini-3-pro`, all on the Responses wire. So no `verified_models` list is needed, unlike codex. The gate checks the exact set every time.
- A tool name that the server doesn't list, such as the doc tools when no repo sets `read_paths`, only prints a notice. The run goes on.
- `--disable-builtin-mcps` drops Copilot's own GitHub MCP server. The GitHub tools come only from the pinned `github-mcp-server`, with `--read-only` and `--tools` for the kind, as in the other presets.
- **No resource-read tools,** on any kind. The execution run was offered exactly `get_work`, `record_result` and the five GitHub tools. A scripted `read_mcp_resource` call got "does not exist". So Copilot can serve execution, unlike codex.

**Unlisted tools are denied without prompting.**
- A scripted `bash` call got "Tool 'bash' does not exist", wrote no file, and the run exited 0 on its own.
- A tool that's offered but not in `--allow-tool`, and isn't marked read-only, was denied with "Denied, no one to approve". The run didn't wait.
- One caution: Copilot runs an offered tool marked read-only without asking, even if it isn't in `--allow-tool`. That's harmless here because `--available-tools` already limits the offered set. But it means `--available-tools`, not `--allow-tool`, is the boundary, and the gate tests it that way.
- `--allow-tool` per tool works, as in `jotflow(get_work)`. The preset lists each tool rather than whole servers.

**Transcripts and memory.**
- Memory is off in prompt mode unless `--enable-memory` is passed, and the preset never passes it.
- Copilot always writes a session: `session-state/`, `session-store.db`, `logs/` and `config.json`. The preset sets `COPILOT_HOME` to `<run dir>/copilot`, so all of it is removed with the run. The `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_DATA_HOME` variables point into the run directory too.
- Without the `XDG_*` variables, Copilot unpacks a package cache into `$HOME/.cache/copilot`. With them, nothing was written to `HOME`, and a run still took about 2 seconds.
- Node's compile cache lands in `TMPDIR`, which the runner already sets to the run directory.

**Login: bring your own key, offline.**
- `COPILOT_OFFLINE=true` turns off GitHub authentication, telemetry, web tools, the built-in GitHub MCP server and auto-update.
- The model is reached through `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_TYPE`, `COPILOT_PROVIDER_WIRE_API` and `COPILOT_PROVIDER_API_KEY`, which the team lists in `JOTFLOW_AGENT_ENV_ALLOW`.
- Offline mode still reached JotFlow's HTTP MCP server. With a remote provider URL, it tried to connect to that URL, so a hosted provider works as well as a local one.
- The model id comes from `JOTFLOW_AGENT_MODEL` as `--model`. With BYOK there's no default model, so the preset refuses to start without one.

**Env names the preset refuses.** The runner already forbids `GH_*`, `GITHUB_*` and `*_TOKEN` names, so `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` and `GITHUB_TOKEN` can't reach a run. On top of that, the copilot preset makes the runner refuse to start if `JOTFLOW_AGENT_ENV_ALLOW` lists any of these:
- `COPILOT_ALLOW_ALL`, because exactly `true` also trusts the working directory and loads its hooks
- `COPILOT_PROVIDER_API_KEY_COMMAND`, which runs a shell command
- `COPILOT_HOME`, `COPILOT_OFFLINE` and the `XDG_*` names, which the preset sets
- `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, `COPILOT_EDITOR`, `OTEL_*` and `COPILOT_OTEL_*`

This needs a small runner change: a preset's optional `refused_env` set, checked at startup like `run_kinds`.

**Run kinds:** drafting, question and execution. A question run gets no GitHub server, as for every preset (#57).

**Neither token reaches the model.** In every run, the run token and the GitHub token were absent from all the model requests. Both live only in the 0600 `mcp.json`.

## Gemini CLI: locked down, but deferred

**Why deferred.** JotFlow's MCP server serves only the stateless MCP 2026-07-28 revision (`PINNED_VERSIONS` in `whiteboard/server.py`). Gemini CLI 0.65.0 bundles `@modelcontextprotocol/sdk` 1.23.0, whose newest version is 2025-11-25, and so do 0.66.0-preview.0 and the 0.67.0 nightly. JotFlow's server answered Gemini's first request with a version error, so Gemini saw no JotFlow tools. Even the latest v1 SDK, 1.32.1, stops at 2025-11-25. The new revision is in the separate v2 client package, and Gemini hasn't moved to it. No upstream issue or PR tracks the move. Gemini does connect to the pinned `github-mcp-server`, so JotFlow's server is the only blocker.

**Alternatives, and why this plan doesn't take them:**
- **Serve older MCP versions from JotFlow.** The handshake era brings sessions back, which #16 would have to bind to tokens. That's a server-wide change made for one CLI.
- **A stdio bridge in the runner** that speaks 2025-11-25 to Gemini and 2026-07-28 to JotFlow. It would hold the run token in another process, and add a component to review and pin, also for one CLI.
- **Recommendation:** open a follow-up issue for the Gemini preset, blocked on Gemini's MCP client, with the recipe below. Revisit at each Gemini upgrade, or if the lead prefers the bridge.

**The recipe, verified on 0.65.0 with stub MCP servers speaking Gemini's version and with the real GitHub server:**
- **Pin and install:** `@google/gemini-cli@0.65.0` in `AGENT_NPM_PACKAGES`, and `gemini --version` prints `0.65.0`.
- **Command:** `gemini --skip-trust -m <model> -e none --allowed-mcp-server-names jotflow --allowed-mcp-server-names github --admin-policy <run dir>/policy/jotflow.toml -p <prompt> -o json`. The flag must be repeated, not space-separated, or the second name is read as a prompt. `--skip-trust` is needed, because headless Gemini refuses an untrusted folder, and the run directory holds only the run's own files. A model must be named, because without one Gemini first calls a routing model.
- **Locked to the kind's tools.** `tools.core` must list the exact MCP tool names, such as `mcp_jotflow_get_work`. Then the model was offered exactly those, and `read_file` and `run_shell_command` got "not found". `tools.core = []` hides MCP tools too. Without `tools.core`, Gemini offers file reads, grep, glob, `google_web_search`, `read_mcp_resource` and `invoke_agent`, even with `experimental.enableAgents` off. Each server also gets `includeTools`, and `mcp.allowed` plus `--allowed-mcp-server-names` drop any other server: a third configured server never loaded.
- **Denied without prompting.** An admin policy that denies `*` at low priority and allows the kind's tools removes every other tool from the offered set. An offered, allowed tool ran with no prompt, even when not marked read-only.
- **Transcripts and memory.** `GEMINI_CLI_HOME=<run dir>/gemini` keeps settings, the chat transcript (`tmp/<project>/chats/*.jsonl`) and logs in the run directory, and nothing was written to `HOME`. The lab's settings also turned off auto-update, usage statistics, telemetry, checkpointing, plan mode, skills, hooks, agents, auto memory and the directory tree in the model's context. Each of those still needs its own check in a gate. `save_memory` is never in `tools.core`.
- **Login:** `GEMINI_API_KEY`, with `security.auth.selectedType = "gemini-api-key"`. Other Google logins weren't tried.
- **Kinds:** with `tools.core` there are no resource-read tools, so all three would be possible.

## Design, for the copilot preset

- **`CopilotPreset`** in `adapters/presets.py`, next to `CodexPreset`. `plan()` writes `mcp.json` in Copilot's shape: `type: "http"` with `headers` for JotFlow, and `type: "local"` with `command`, `args`, `env` and `tools` for GitHub. It returns the argv above, the env above, and `github_token_env=False`. `check_install()` checks the exact version. `default_models` is empty, and the runner refuses a copilot runner with no model. `run_kinds` is all three.
- **Provider types.** The runner refuses a copilot runner whose `COPILOT_PROVIDER_TYPE` isn't `openai` or `azure`, and one with none set, since Copilot then falls back to its GitHub login.
- **`refused_env`,** an optional preset member. The runner reads it at startup and refuses to start if `JOTFLOW_AGENT_ENV_ALLOW` names any of those names. Codex and claude-code don't set it.
- **`PRESETS` and `EXPERIMENTAL`** gain `copilot`. `PARTIAL_PRESET_KINDS` is unchanged, because copilot serves every kind.
- **The gate.** `tests/test_copilot_gate.py`, in a new "Copilot preset gate" CI job with the CLI from a lockfile in `.github/ci/copilot`. It reuses `FakeResponsesApi`, extended so its stream carries the full event sequence and the output in `response.completed`, which Copilot needs. For each of the three kinds it checks:
  - the exact offered set
  - a scripted `bash` call refused, writing nothing
  - an offered but unallowed write tool denied without a prompt, for the drafting run, with its allow list narrowed for the test
  - JotFlow's audit log showing `tools/list` served
  - neither token in any model request
  - the CLI exiting by itself
- **Docs:** a copilot section in presets.md, agent-adapters.md's list of presets and `refused_env`, the threat model's preset row, and the CHANGELOG.
- **`docker-compose.yml`** keeps its default `AGENT_NPM_PACKAGES`. A team adds `@github/copilot@1.0.101` to try the preset, as with codex before it was published.

## Decisions

The lead decided on 2026-10-21, following this plan's recommendations:
1. **Gemini is deferred** to #286, which records the recipe and the MCP-version blocker. There's no bridge.
2. **Copilot uses bring-your-own-key only.** The runner keeps refusing a third GitHub token. #287 is a discussion of the subscription login.
3. **OpenAI-compatible providers only.** The preset fails closed unless `COPILOT_PROVIDER_TYPE` is `openai` or `azure`, until #288 gates Anthropic's API.

So #59 is scoped to the copilot preset, and the implementation PR says Fixes #59.

## Risks and open questions

1. **Decided: Copilot's own login is out for now (#287).** Most teams would use Copilot with their Copilot subscription, which is a GitHub token (`COPILOT_GITHUB_TOKEN`) for a licensed account. The runner refuses `*_TOKEN` and `GITHUB_*` names, rightly, so this plan supports only bring-your-own-key. Allowing a third GitHub token into the agent container would need its own review: what it can reach, and that `--disable-builtin-mcps` keeps it away from the model's tools. Proposed: BYOK only now, and a separate issue for the subscription login if teams ask.
2. **Decided: the Anthropic wire waits for #288.** The fake model speaks only the OpenAI Responses API, so the gate covers `COPILOT_PROVIDER_TYPE=openai` with `WIRE_API=responses`, plus Azure, which uses the same wire. The tool set is filtered by the CLI, not the provider, but that's unproven for the Anthropic wire. Proposed: the preset accepts `openai` and `azure` only, until the gate gains a fake Anthropic Messages API, in a follow-up.
3. **Copilot runs read-only-marked tools unasked** when they're offered but not in `--allow-tool`. It's bounded by `--available-tools`, and the gate tests that boundary. A later Copilot release could change either behaviour, so the pin and the gate must move together.
4. **Release cadence.** Copilot ships almost daily. The pin is exact, so nothing moves without a PR that reruns the gate.
5. **Decided: Gemini is deferred to #286.**

## Progress checklist

- [x] The lead approves this plan and decides open questions 1, 2 and 5
- [ ] Security approves it
- [x] Commit this plan and open the draft PR
- [x] Open the follow-ups: #286 (Gemini), #287 (subscription login), #288 (Anthropic's API)
- [ ] `CopilotPreset`, `refused_env` in the runner, and `PRESETS` and `EXPERIMENTAL`
- [ ] Unit tests: argv and `mcp.json` pinned per kind, no run data in the prompt or argv, no token in argv or env, `check_install()`, a model required, `refused_env` refusals, and an end-to-end run with a fake CLI
- [ ] The gate, its lockfile, and the CI job
- [ ] The Dockerfile drops the musl binary when Copilot is installed
- [ ] presets.md, agent-adapters.md, the threat model, the CHANGELOG
- [ ] `jotflow replay --all --agent scripted` and `--execute` pass
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- The lab runs above, on 2026-10-21, with Gemini CLI 0.65.0 and Copilot CLI 1.0.101.
- Once built: the gate in CI, and `make check`.

## Handoff notes

- Copilot's tool names are `<server>-<tool>`, such as `jotflow-get_work`. Gemini's are `mcp_<server>_<tool>`.
- Copilot needs the fuller Responses stream. With only `response.output_item.done` and a bare `response.completed`, it reports "No response was returned".
