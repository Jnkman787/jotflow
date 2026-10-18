# Agent presets: Claude Code first, then Codex CLI

## Links and status

- Issue: #22
- Draft PR: #137
- Status: ready for review
- Last verified: Claude Code 2.1.303 and github-mcp-server 2.0.2, checked locally on 2026-10-18 (see Validation)
- Next safe action: review #137; then the Codex CLI preset in a second PR under this plan

## Objective and done-when

A team names a preset in the agent container's env file, `JOTFLOW_AGENT_PRESET=claude-code`, and gets a tested, locked-down command for each run kind. This replaces the raw `JOTFLOW_AGENT_COMMAND` template from #54 as the normal path. The raw template stays as the documented escape hatch for unsupported CLIs, and is the team's responsibility, as architecture.md already says.

This PR delivers the `claude-code` preset. Codex CLI follows in a second PR under this same plan, so this PR says Refs #22, not Fixes.

From the issue, refined:

- [ ] Each preset is verified against the current CLI release: Claude Code 2.1.303 in this PR, Codex in the next one
- [ ] A preset's MCP config is written to a temp file and cleaned up
- [ ] Adding a preset needs no change to the orchestrator
- [ ] Per-run-kind tool allowlists. Drafting and question runs get read-only GitHub access and no shell or file-write tools. Execution runs get write access for one issue.
- [ ] Session transcripts are off
- [ ] Headless permission prompts are covered: nothing prompts, and anything not allowed is denied
- [ ] Docs say how the model login gets into the agent container
- [ ] Docs say where the CLI reads its MCP config and token, and the check confirms the CLI doesn't echo the token into its logs or transcripts
- [ ] Only JotFlow plus the pinned GitHub MCP server are loaded, with strict MCP config, and the GitHub server is pinned by version and checksum
- [ ] Execution runs allow only the one create or update tool, plus `record_result`, plus the read tools that the marker and label checks need
- [ ] The model can be set per run kind

**Out of scope:**

- Gemini CLI and Copilot CLI, which moved to M5.
- Spend caps. Claude Code has `--max-budget-usd`, but the runner's time limit and the turn limit already bound a run. A cap can follow if live runs show a need.
- GitHub Enterprise hosts.
- Changing what JotFlow sends to the runner, which stays run id, kind, and token (D6).

## Design

### Where the preset is chosen

The issue says presets are selectable in `jotflow.toml`. D6 and #54 moved the agent command out of JotFlow, and the runner never reads JotFlow's config. So the preset is named in `agent.env` and `agent-exec.env`, next to the tokens it governs. JotFlow and the orchestrator stay unchanged, which is also the third done-when box. I'll update the issue text to match.

The runner gets these settings:

- **`JOTFLOW_AGENT_PRESET`** names a preset. Setting both this and `JOTFLOW_AGENT_COMMAND`, or neither, is a startup error.
- **`JOTFLOW_AGENT_MODEL`** is the model for every kind this runner serves. `JOTFLOW_AGENT_MODEL_DRAFTING`, `_QUESTION`, and `_EXECUTION` override it per kind. Values are checked against a conservative pattern of letters, digits, `.`, `-`, `_`, `:`, `[`, and `]`, up to 100 characters. When unset, the preset's default applies: `sonnet` for drafting and question, and `haiku` for execution, as architecture.md suggests. With the raw command, the model settings are an error, since the runner can't place them.
- **`JOTFLOW_GITHUB_MCP_COMMAND`** is the path of the GitHub MCP server. It defaults to the binary the image installs, `/usr/local/bin/github-mcp-server`.

### The preset interface (`src/jotflow/adapters/presets.py`)

```python
@dataclass(frozen=True)
class RunContext:  # everything a preset may use, nothing else
    kind: RunKind
    run_id: str
    run_dir: Path  # the run's 0700 directory, removed after the run
    config_path: Path  # run_dir / "mcp.json", written 0600 by the runner
    mcp_url: str
    mcp_token: str  # the run's JotFlow token
    github_token: str  # the one token for this kind
    model: str | None
    github_mcp: str


@dataclass(frozen=True)
class RunPlan:
    argv: tuple[str, ...]
    config: Mapping[str, Any]  # written to config_path
    env: Mapping[str, str]  # extra env, on top of the runner's allowlist
    github_token_env: bool  # whether GITHUB_TOKEN and GH_TOKEN are set


class Preset(Protocol):  # read-only properties in the code
    name: str
    default_models: Mapping[RunKind, str]
    needs_github_mcp: bool  # the runner checks the binary at startup
    github_token_in_config: bool  # the runner checks the token's characters

    def plan(self, run: RunContext) -> RunPlan: ...


PRESETS: Mapping[str, Preset] = {"claude-code": CLAUDE_CODE}
```

The raw command becomes a `CommandPreset` built from `JOTFLOW_AGENT_COMMAND`. It keeps today's behavior exactly: the JotFlow-only config, `{mcp_config}` and `{run_id}` filled in, and the GitHub token in the env. `Runner._run` then always asks the config's preset for a plan, writes `plan.config` 0600, and passes `plan.env` through `child_env`. Adding a preset means adding a class and a registry entry, with no orchestrator or runner change.

Preset env may not override the runner's own names, such as `HOME`, `TMPDIR`, or `JOTFLOW_*`. It also may not use names that `_FORBIDDEN_ENV` rejects. A test enforces both.

### The `claude-code` preset

The argv for a run of kind K:

```
claude -p <K's prompt>
  --mcp-config <config_path> --strict-mcp-config
  --tools ""                      # no built-in tools: no Bash, no file read or write, no web
  --allowedTools <K's MCP tools>  # comma-separated, mcp__<server>__<tool>
  --permission-mode dontAsk       # nothing prompts; anything not allowed is denied
  --no-session-persistence        # no transcript under ~/.claude/projects
  --setting-sources ""            # ignore user, project, and local settings and their hooks
  --disable-slash-commands
  --max-turns <40 drafting, 20 question, 15 execution>
  --model <model>
```

The prompts are short, fixed strings in the preset. They tell the agent to call `get_work` first and follow its guidance. The guidance itself lives in JotFlow's tool descriptions, which already mark Slack text as data and describe the marker check. Untrusted text never appears in argv.

The MCP config, written 0600 by the runner:

```json
{"mcpServers": {
  "jotflow": {"type": "http", "url": "<mcp_url>", "headers": {"Authorization": "Bearer <run token>"}},
  "github": {"type": "stdio", "command": "/usr/local/bin/github-mcp-server",
             "args": ["stdio", "--read-only", "--tools", "<K's GitHub tools>"],
             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "<K's GitHub token>"}}}}
```

`--read-only` appears only for drafting and question runs. The GitHub server's read-only mode takes priority over `--tools`, so even a mistaken tool list can't expose a write there.

The tools for each kind:

| Kind | JotFlow tools | GitHub tools |
|---|---|---|
| drafting | `get_work`, `list_topics`, `get_topic`, `propose_issue`, `propose_update`, `link_message`, `ask_channel` | `issue_read`, `list_issues`, `search_issues`, `list_label`, `get_label` |
| question | `get_work`, `list_topics`, `get_topic`, `ask_channel` | the same read tools |
| execution | `get_work`, `record_result` | `issue_read` and `list_issues` for the marker check, `get_label` for the label check, `issue_write` for a create or edit, and `add_issue_comment` for an update's comment |

The JotFlow column is checked by a test against the kinds the whiteboard tools actually declare, so the two can't drift. "Only the one create or update tool" from the security review becomes `issue_write`, which covers both, plus `add_issue_comment`, because an update proposal may carry a comment. Both write tools are scoped by the token to the mapped repos. JotFlow's `record_result` check rejects a URL for any other repo.

The tool restriction has three layers:

1. The GitHub server only exposes the kind's tools.
2. JotFlow lists only the kind's tools, and refuses other calls the same way it refuses a tool that doesn't exist.
3. Claude Code has no built-in tools and denies any MCP tool not in `--allowedTools`.

The preset's extra env:

- **`XDG_CACHE_HOME=<run_dir>/cache`.** Claude Code writes each MCP server's stderr to a log under the cache directory. Pointing it into the run directory deletes those logs with the run, instead of letting them pile up in the home volume.
- **`DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.** These keep the CLI from sending telemetry or error reports, and from updating itself.

`github_token_env` is false. The GitHub token reaches only the GitHub MCP server, through the 0600 config, so it is no longer in Claude Code's own environment. With no shell tool that environment can't be read by the agent anyway, but leaving it out removes one copy. This changes runner behavior that security reviewed in #54, so I'm raising it with them.

The model login: `ANTHROPIC_API_KEY` in the env file, passed through `JOTFLOW_AGENT_ENV_ALLOW` as today. Alternatively, a one-time `claude login` inside each service. Its login is kept in that service's home volume, and `--setting-sources ""` doesn't affect it. The docs cover both.

### Config checks at startup

- **The GitHub token's characters.** The token must match `[A-Za-z0-9_]+`, which covers both classic and fine-grained tokens. Claude Code expands `${VAR}` in MCP configs, so a token with `$` or braces could be rewritten. JotFlow's own run tokens are already limited to URL-safe characters.
- **The GitHub MCP binary.** For a preset that needs it, the binary must exist and be executable.

### The image (`Dockerfile.agent`)

- **The GitHub MCP server.** It installs from the v2.0.2 release tarball for `TARGETARCH`, `amd64` or `arm64`. Each tarball is checked against a sha256 written into the Dockerfile, taken from the release's checksum file, and the build fails on a mismatch. The binary goes to `/usr/local/bin/github-mcp-server`. Its version and checksums are build arguments, so an upgrade is a one-line change with a recorded checksum.
- **Claude Code.** The documented `AGENT_NPM_PACKAGES` value becomes `@anthropic-ai/claude-code@2.1.303`, and Compose sets it as a build argument for the agent service. CI's runner-only image keeps an empty value, so CI doesn't need npm downloads, but it does install and run `github-mcp-server --version`.

### Docs

- `agent.env.example` and `agent-exec.env.example` lead with `JOTFLOW_AGENT_PRESET=claude-code` and the model settings, and keep the raw command commented out as the escape hatch.
- architecture.md, in the agent-container section, gets the preset table, the three restriction layers, the token and config locations, and the transcript and log notes.
- A short `docs/presets.md` lists each preset's argv, config, tools, verified versions, login options, and the manual check below, so the next CLI bump can rerun it.

### Alternatives considered

- **`--bare`.** It also skips hooks, plugins, and CLAUDE.md, but it accepts only `ANTHROPIC_API_KEY` and never an OAuth login. `--setting-sources ""` with `--strict-mcp-config` and `--tools ""` gets the same isolation and keeps both login options.
- **`--restricted`.** It removes code-running tools but keeps file tools. `--tools ""` is stricter.
- **GitHub's remote MCP server.** It would need the token in an HTTP header to github.com and gives no version pin. The local binary is pinned by checksum and talks to the API directly.
- **The preset in `jotflow.toml`.** This would need JotFlow to send a command or a preset name to the runner, which D6 forbids.

## Risks and open questions

- **Coordination with security.** #131, the runner client, depends only on the runner's HTTP surface, which this PR doesn't change. The env change, which moves the GitHub token out of the agent's env for presets, and the new settings go to security for review.
- **Claude Code flag drift.** The pinned version and the documented check in `docs/presets.md` cover this. A test pins the exact argv, so a change is deliberate.
- **The `dontAsk` denial of unlisted MCP tools.** This is verified from the CLI's help and documentation, not by a live model call in CI. Layers 1 and 2 hold even if layer 3 changes. The live run on the maintainer's deployment exercises it.
- **Claude Code still writes `~/.claude.json`** in the home volume: startup counters and ids, with no tokens, as verified below. That is accepted.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `presets.py`: `RunContext`, `RunPlan`, `CommandPreset`, `ClaudeCodePreset`, and the registry
- [x] Runner: the preset or command setting, the model per kind, the GitHub MCP path, token character checks, and `_run` using the plan
- [x] `Dockerfile.agent`: the checksum-pinned GitHub MCP server, and the Claude Code pin in the docs and Compose
- [x] Tests: the exact argv per kind, the config shape and 0600 mode, cleanup, env rules, the JotFlow tool table against the whiteboard kinds, config errors, and that the orchestrator doesn't import presets
- [x] Docs: env examples, architecture.md, and `docs/presets.md`
- [ ] `make check` passes
- [ ] Mark the PR ready for review
- [ ] Second PR: the Codex CLI preset

## Validation

On 2026-10-18 I ran Claude Code 2.1.303 with the drafting argv above, a scratch HOME, an invalid API key, and an MCP config with an unreachable JotFlow URL plus github-mcp-server 2.0.2. The `stream-json` init event showed:

- the tool list was only the five GitHub read tools, with no built-in tools
- the permission mode was `dontAsk`
- no slash commands or skills were loaded
- the model `haiku` resolved
- only the two configured servers were loaded

After the run:

- no transcript was written under `~/.claude/projects`
- a sentinel JotFlow token and a sentinel GitHub token appeared in none of stdout, stderr, `~/.claude.json`, or the MCP logs
- the MCP logs moved into the run directory once `XDG_CACHE_HOME` was set

I ran github-mcp-server 2.0.2's `tools/list` directly:

- `--read-only --tools issue_read,list_issues,search_issues,list_label,get_label,issue_write,add_issue_comment` listed only the five read tools, which confirms that read-only takes priority
- without `--read-only`, the execution tool list listed exactly those five tools

The release tarball for Linux x86_64 matched its published sha256.

During implementation I built the agent image with `AGENT_NPM_PACKAGES=@anthropic-ai/claude-code@2.1.303` and ran the real drafting and execution plans from `ClaudeCodePreset` inside it. The init event matched the table above for both kinds. The run used the `sonnet` model for drafting and `haiku` for execution, and showed version 2.1.303. No sentinel appeared in the output or under `HOME`.

That run turned up an empty `~/.claude/projects/<run dir>/memory` directory for each run. The preset now sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, and with it no projects directory is created. ADD's `--checksum` checks the GitHub MCP server tarball, and the image runs `github-mcp-server --version` as a build step.

**After review started:** the lead forwarded docs research recommending `--max-turns`, since Claude Code has no default, and `DISABLE_UPDATES`. Both are added. `--max-turns` is accepted by 2.1.303 although `--help` doesn't list it. Two other research points need no change. First, the research's warning that `${ANTHROPIC_API_KEY}` and similar names expand to empty in headers doesn't apply, because the runner writes literal tokens and refuses GitHub tokens with `$` or braces. Second, the sub-agent tool (Agent or Task) is absent from the init tool list under `--tools ""`.

## Handoff notes

None yet.
