# Agent presets

To add a preset of your own, or check one, see [agent-adapters.md](agent-adapters.md).

A preset is how the agent container's runner starts an agent CLI for one run. JotFlow ships two, `claude-code` and `codex`, and the team picks one; `jotflow init` offers both. Setup names the preset in `agent.env` and `agent-exec.env`, or by hand:

```
JOTFLOW_AGENT_PRESET=claude-code
```

The preset sets the CLI's flags, the MCP config, which tools each run kind may use, and the model per run kind. The design and the tool table are in [architecture.md](architecture.md), under "Presets". For a CLI without a preset, set `JOTFLOW_AGENT_COMMAND` instead. The tool limits of a custom command are the team's responsibility.

| Preset | CLI | Verified with | GitHub MCP server |
|---|---|---|---|
| `claude-code` | Claude Code, `@anthropic-ai/claude-code` | 2.1.303, 2026-10-18 | github-mcp-server 2.0.2 |
| `codex` | Codex CLI, `@openai/codex` | 0.163.0, 2026-10-19, by the CI tool-set gate; no real-model pass recorded yet | github-mcp-server 2.0.2 |

## claude-code

**The command**, for a run of each kind:

```
claude -p <the kind's fixed prompt>
  --mcp-config <run dir>/mcp.json --strict-mcp-config
  --tools ""
  --allowedTools <the kind's tools, as mcp__jotflow__<tool> and mcp__github__<tool>>
  --permission-mode dontAsk
  --no-session-persistence
  --setting-sources ""
  --disable-slash-commands
  --max-turns <40 drafting, 20 question, 15 execution>
  --model <model>
```

- **`--tools ""`** removes every built-in tool, so the agent has no shell, file, or web access.
- **`--strict-mcp-config`** loads only the two servers in the run's config, and none from user or project settings.
- **`--permission-mode dontAsk`** means nothing ever prompts. A tool not on `--allowedTools` is denied.
- **`--no-session-persistence`** means no transcript is written.
- **`--setting-sources ""`** reads no user, project, or local settings, and so no hooks from them.
- **`--max-turns`** stops a looping agent. Claude Code has no default limit. The runner's time limit still ends any run. The flag works in 2.1.303 although `--help` doesn't list it.
- **The prompt** is fixed, per kind, and tells the agent to call `get_work` first. No Slack text is ever in the command line.

The preset also sets these variables:

- **`XDG_CACHE_HOME`** is set to a `cache` folder in the run's directory, so the CLI's per-server MCP logs are removed with the run.
- **`CLAUDE_CODE_DISABLE_AUTO_MEMORY`** keeps the CLI from writing memory files that a later run could load.
- **`DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER`, `DISABLE_UPDATES`, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`** keep the CLI from sending telemetry or error reports, and from updating itself.

**How the agent reaches GitHub.** claude-code reaches GitHub only through the pinned github-mcp-server in the agent image. Drafting and question runs use the read-only token with `--read-only`, and execution runs use the write token with the execution tools. The team configures only the two fine-grained tokens, in `agent.env` and `agent-exec.env`. Execution runs copy the exact writes JotFlow plans (#31), so the model never composes an issue body. The team doesn't need to set up `gh` or any other GitHub tool.

**What the write token can reach.** An execution run's `issue_write` and `add_issue_comment` can create, edit, close, relabel, or comment on any issue in any repo the write token reaches. JotFlow checks only the issue URL the agent reports to `record_result`, after the write. So the write token is the real limit. Make it a fine-grained token limited to exactly the repos your channels feed, with Issues read and write and no other permission. Make the read-only token the same way, with Issues read-only and no other permission. Without `read_paths`, no drafting or question tool reads repository files, and labels are read under Issues. A repo that sets `read_paths` (#209) needs Contents read-only on the read token too, which covers every file in every repo that token reaches; only JotFlow's own doc tools are limited to the allowed paths. GitHub adds Metadata read-only to both tokens by itself.

**Public GitHub is read too.** A drafting run's `search_issues` searches all of public GitHub, not only the token's repos, so its results are another source of untrusted text. Drafts still need approval before anything is written.

**Where the tokens are.** The runner writes the run's MCP config to `/tmp/jotflow-run-<random>/mcp.json`. The file is mode 0600, in a 0700 directory, and is removed when the run ends. It holds two servers:

- `jotflow`, over HTTP, with the run's JotFlow token as an `Authorization` header
- `github`, `/usr/local/bin/github-mcp-server stdio`, with the service's GitHub token as `GITHUB_PERSONAL_ACCESS_TOKEN` in that server's own environment

Neither token is in the command line or in the CLI's environment. The runner refuses a GitHub token with characters a GitHub token never has, because Claude Code expands `${...}` in MCP configs. GitHub's server starts with `--read-only` for drafting and question runs, and with `--tools` set to the kind's GitHub tools for every kind.

**The model.** By default, drafting and question runs use `claude-sonnet-5-5` and execution runs use `claude-haiku-5-5`. Set `JOTFLOW_AGENT_MODEL` for every kind a service serves, or `JOTFLOW_AGENT_MODEL_DRAFTING`, `_QUESTION`, or `_EXECUTION` for one kind. The value is a full model id, such as `claude-sonnet-5-5`, or an alias.

**Use full model ids.** Each Claude Code release resolves an alias such as `sonnet` or `haiku` itself, so the model depends on the CLI version. At the pinned 2.1.303, `haiku` picks Haiku 4.5, not Haiku 5.5. A full id names one model on every version. Both default ids were checked against 2.1.303 on 2026-10-19 with one minimal call each, and the CLI reported the same id back.

**The model login.** Use either of these:

- **An API key.** Put `ANTHROPIC_API_KEY` in each env file and list it in `JOTFLOW_AGENT_ENV_ALLOW`, as the examples do. Use separate keys for the two services if you want to track spend per run kind.
- **A one-time login inside each service**, kept in that service's home volume:

  ```
  docker compose run --rm --no-deps agent claude auth login
  docker compose run --rm --no-deps agent-exec claude auth login
  ```

  Variables ending in `_TOKEN` can't be allowlisted, so `CLAUDE_CODE_OAUTH_TOKEN` can't be passed in. Use the login instead.

**What was checked.** On 2026-10-18, I ran Claude Code 2.1.303 in the agent image with the drafting and execution plans, a sentinel JotFlow token, a sentinel GitHub token, and an invalid API key. The CLI's `stream-json` init event showed the following:

- **Drafting:** only the five read-only GitHub tools.
- **Execution:** those reads plus `issue_write` and `add_issue_comment`.
- **Every run:** no built-in tools, the `dontAsk` permission mode, the expected model, and only the two configured servers. The JotFlow server was unreachable in the check, so it listed no tools.

After those runs:

- Neither sentinel appeared in the CLI's output, its logs, or any file in its home.
- No transcript or memory directory was created.
- The only file the CLI leaves in its home is `~/.claude.json`, which holds startup counters and ids.

Asking github-mcp-server 2.0.2 for its tools with `--read-only` and a tool list that named `issue_write` still listed only read tools.

**Repeat the check after upgrading the CLI or the GitHub server.** First change the pinned versions in `docker/docker-compose.yml` and `docker/Dockerfile.agent`, and the checksums from the GitHub server release's checksums file. Then rebuild:

```
docker compose build agent
```

Then plan a run with the preset and start it with `--output-format stream-json --verbose` added. Use a scratch run directory and `ANTHROPIC_API_KEY=invalid`. Confirm the following in the `init` event and in the home volume:

- the `tools` list
- `permissionMode` is `dontAsk`
- `mcp_servers` lists only `jotflow` and `github`
- neither token appears in the output or under `HOME`

Update the table above with the new versions and date.

## codex

**What's verified.** The security gate below runs Codex 0.163.0 on every pull request, with this preset's own config, and checks its tool set for each run kind it serves. A real-model pass, `jotflow replay --agent codex`, hasn't been recorded yet. "Not yet confirmed", at the end of this section, lists what one would check.

Codex has one limit that claude-code doesn't, and one extra check. Both are enforced at runner startup.

**Limit: drafting and question runs only.** Codex keeps its MCP resource tools (`read_mcp_resource`, `list_mcp_resource_templates`), which can pull any repository text into the agent's context. That's acceptable for drafting, whose input is untrusted anyway. An execution run holds the write token and must see only the approved fields, so the codex preset refuses execution: an `agent-exec` runner set to `codex` won't start. Use `claude-code` in `agent-exec.env`. Both agent services share one image, so the `codex` CLI is installed in `agent-exec` too. The runner refuses the codex preset for execution there, so only a team's own `JOTFLOW_AGENT_COMMAND` in `agent-exec.env` could start it, and a custom command's tools are the team's responsibility.

**The startup probe: no model list.** Newer models can add a code-execution tool. Codex 0.163.0 runs most current models (the gpt-6 family and gpt-5.6) in "code mode", which adds a JavaScript `exec` tool, with other tools nested inside it, and a sub-agent namespace (`spawn_agent`, `send_message`, and others), whatever the config says. Which models do is set by Codex's model catalog, not by JotFlow's config. So the runner asks Codex itself (#220). At startup, for each model it's configured with, it runs the pinned Codex once with this preset's own config against a local fake model API, and reads the request Codex would send. Every tool offered must be one of the five built-ins below, and no nested namespace may appear. Otherwise the runner refuses to start, naming the extra tools. `gpt-5.5`, the default, passes. Each probe takes about a second and calls no real model. It runs at startup only: with the remote model catalog off and the CLI version pinned, nothing can change the tool set before a run. The probe routes Codex to a local custom provider, while runs use OpenAI's. Both read the release's bundled catalog, so the tools match, and the threat model names the difference.

**Set it up.** Type `codex` at setup's agent preset question. Setup writes `JOTFLOW_AGENT_PRESET=codex` in `agent.env`, and keeps `agent-exec.env` on `claude-code`, as the first limit needs. With a hand-built image, install both CLIs, since the execution runner still runs claude-code: `AGENT_NPM_PACKAGES="@anthropic-ai/claude-code@2.1.303 @openai/codex@0.163.0"`. The published agent image already has both. A runner set to either preset won't start if its CLI is missing.

**The model login is an API key only.** Setup asks for an OpenAI API key and saves it in `agent.env` as `CODEX_API_KEY`, listed in `JOTFLOW_AGENT_ENV_ALLOW`. By hand, `OPENAI_API_KEY` works too. `agent-exec.env` keeps claude-code's login, `ANTHROPIC_API_KEY` or a Claude account login. Each run gets its own `CODEX_HOME`, so a ChatGPT login saved by `codex login` in the home volume isn't seen.

**The runner fails closed.** At startup it checks that `codex --version` is exactly 0.163.0 and that `codex features list` still has `mcp_2026_07_28`. Otherwise it refuses to start, naming the problem.

**The command,** for a run of each kind:

```
codex exec --ephemeral --skip-git-repo-check --ignore-rules --color never <the kind's fixed prompt>
```

The command carries no run data. `CODEX_HOME` points at `<run dir>/codex` (0700), which holds `config.toml` (0600). That config holds the following:

- **The JotFlow server,** with the run's token as an `Authorization` header, and **the pinned GitHub server,** with the GitHub token in its own environment. Each server is limited to the kind's tools with `enabled_tools`, set to `default_tools_approval_mode = "approve"`, and `required`. GitHub's server also has its own `--read-only` and `--tools` flags, as with claude-code.
- **`approval_policy = "never"` and `sandbox_mode = "read-only"`,** so nothing prompts and no file can be written.
- **Off:** `web_search`, update checks, history (`persistence = "none"`), analytics, feedback, and `request_user_input`.
- **`[features]`:** the shell (`shell_tool`, `unified_exec`) and every other built-in that acts outside the run are off: apps, browser and computer use, image generation, multi-agent, plugins, skills, hooks, memories, goals, and the code-mode host. So is `api_key_model_discovery`. With an API key, it fetches OpenAI's model catalog on every run, and that catalog sets each model's tools: a remote change could switch `gpt-5.5` into code mode, with an `exec` tool. Off, Codex uses the catalog bundled with the pinned release (#284).
- **`mcp_2026_07_28 = true`.** JotFlow serves only MCP 2026-07-28, and Codex speaks it only with this flag, which Codex 0.163.0 marks "under development". Without the flag, Codex sees no JotFlow tools.

Codex keeps its state, logs, and memory databases in `CODEX_HOME`, so the runner removes them with the run. Neither token is in the command line or the environment.

**The security gate.** `tests/test_codex_gate.py` runs in CI's "Codex preset gate" job, with Codex 0.163.0 from `.github/ci/codex`'s lockfile. Codex runs this preset's own config, and for every run kind it checks four things:
- for drafting and question runs, the model is offered exactly the five built-ins below, and no shell, `exec`, or `local_shell` tool
- a scripted `apply_patch` call is refused ("writing is blocked by read-only sandbox; rejected by user approval settings"), no file is written, and no prompt holds the run
- JotFlow's MCP server is reached
- neither token reaches the model

A further test runs Codex with this config and OpenAI's own provider, as a deployment does, but in a network namespace with no network. It shows Codex never tries to fetch the remote model catalog, and that it would with `api_key_model_discovery` on. The CI job allows unprivileged namespaces for it.

Codex talks only to `jotflow.testing.fake_responses_api` on 127.0.0.1, so it costs nothing. Further tests show a code-mode model would get `exec` and `spawn_agent`, that the startup probe passes `gpt-5.5` and refuses `gpt-6.1-sol`, and that the pinned GitHub server, fetched by the same checksum as the image, serves no repository content.

**What was checked, on 2026-10-19,** without a model call. Codex 0.163.0 ran this preset's own config against a local fake model API that records the request, with JotFlow's real MCP server and github-mcp-server 2.0.2:

- **JotFlow:** Codex connected. JotFlow's audit shows `server/discover` and `tools/list` served.
- **Built-in tools:** for drafting runs, the request carried only `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`, `apply_patch`, and `tool_search`. There was no shell. Codex reaches MCP tools through `tool_search`.
- **Tokens:** neither sentinel token appeared in the output or the request, and nothing was written to `HOME`. `CODEX_HOME` was 0700 and `config.toml` was 0600.

**Not yet confirmed.** These need a real model run:
- that `tool_search` finds JotFlow's tools. Only GitHub, which sends server instructions, was listed as a search source.
- that MCP calls run without an approval prompt

The `apply_patch` refusal is already shown, by the gate. Whether Codex fetches model metadata from OpenAI is settled. It did, on every run, until #284 turned that off, so `gpt-5.5`'s tools now come from the pinned release's own catalog.

**Codex's MCP resource tools** would let a drafting run read repository files if a server served them, and with `read_paths` (#209) the read token can read any file in any repo it reaches, not only the allowed paths. A question run has no GitHub server (#57). Codex can't turn those tools off. But nothing behind them serves a repository file: JotFlow's own server declares no resources, and the GitHub server, started with this preset's `--read-only --tools` flags, registers no `repo://` templates and refuses a repository read without making a network call. The runner checks that at every start, with a dummy token, and won't start otherwise. So `read_paths` works with `codex` too (#274). A later GitHub server release that served repository content would stop the runner, failing closed. Execution never runs on Codex, so the write token never meets them. Files the drafting run does read, through JotFlow's own doc tools within `read_paths`, are untrusted text, like Slack messages. The drafting instructions treat every field and tool result outside JotFlow's own instructions as data, and drafts still need approval.

