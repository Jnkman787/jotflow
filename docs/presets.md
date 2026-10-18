# Agent presets

A preset is how the agent container's runner starts an agent CLI for one run. Name it in `agent.env` and `agent-exec.env`:

```
JOTFLOW_AGENT_PRESET=claude-code
```

The preset sets the CLI's flags, the MCP config, which tools each run kind may use, and the model per run kind. The design and the tool table are in [architecture.md](architecture.md), under "Presets". For a CLI without a preset, set `JOTFLOW_AGENT_COMMAND` instead. The tool limits of a custom command are the team's responsibility.

| Preset | CLI | Verified with | GitHub MCP server |
|---|---|---|---|
| `claude-code` | Claude Code, `@anthropic-ai/claude-code` | 2.1.303, 2026-10-18 | github-mcp-server 2.0.2 |

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

**Where the tokens are.** The runner writes the run's MCP config to `/tmp/jotflow-run-<random>/mcp.json`. The file is mode 0600, in a 0700 directory, and is removed when the run ends. It holds two servers:

- `jotflow`, over HTTP, with the run's JotFlow token as an `Authorization` header
- `github`, `/usr/local/bin/github-mcp-server stdio`, with the service's GitHub token as `GITHUB_PERSONAL_ACCESS_TOKEN` in that server's own environment

Neither token is in the command line or in the CLI's environment. The runner refuses a GitHub token with characters a GitHub token never has, because Claude Code expands `${...}` in MCP configs. GitHub's server starts with `--read-only` for drafting and question runs, and with `--tools` set to the kind's GitHub tools for every kind.

**The model.** By default, drafting and question runs use `sonnet` and execution runs use `haiku`. Set `JOTFLOW_AGENT_MODEL` for every kind a service serves, or `JOTFLOW_AGENT_MODEL_DRAFTING`, `_QUESTION`, or `_EXECUTION` for one kind. The value is an alias or a full model id, such as `claude-sonnet-5-5`.

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

**Repeat the check after upgrading the CLI or the GitHub server.** First change the pinned versions in `docker-compose.yml` and `Dockerfile.agent`, and the checksums from the GitHub server release's checksums file. Then rebuild:

```
docker compose build agent
```

Then plan a run with the preset and start it with `--output-format stream-json --verbose` added. Use a scratch run directory and `ANTHROPIC_API_KEY=invalid`. Confirm the following in the `init` event and in the home volume:

- the `tools` list
- `permissionMode` is `dontAsk`
- `mcp_servers` lists only `jotflow` and `github`
- neither token appears in the output or under `HOME`

Update the table above with the new versions and date.
