# Agent adapters: adding a preset

This page is for anyone adding support for another agent CLI, or running their own agent with a custom command. It covers what JotFlow's runner already guarantees for every run, what a preset must guarantee on top, and how to prove it before anyone relies on it.

The shipped presets and what each was checked against are in [presets.md](presets.md). The design is in [architecture.md](architecture.md), under "The agent containers" and "Presets". The threats these rules answer are in [threat-model.md](threat-model.md).

## Two ways to start an agent

- **A named preset** (`JOTFLOW_AGENT_PRESET=claude-code`) is code in `src/jotflow/adapters/presets.py`, reviewed and tested like the rest of JotFlow. Adding one needs no change to JotFlow's orchestrator: JotFlow sends the runner only a run id, a run kind, and a token, and the runner turns those into a command.
- **A custom command** (`JOTFLOW_AGENT_COMMAND`) is a JSON list, run without a shell. `{mcp_config}` is replaced by the path of the run's MCP config file and `{run_id}` by the run id. The runner still gives it the private directory, the env allowlist, the token split, and the time limit described below. But which tools the agent can use, whether it has a shell, and what it writes to disk are **the team's responsibility**. JotFlow can't check them. Use a custom command to try a CLI out, and write a preset before relying on it.

A runner takes exactly one of the two.

## What the runner already does for every run

You get all of this for free, whatever the preset. Don't reimplement it.

- **Private files.** Each run gets its own directory, mode 0700, under the runner's workdir. The MCP config is written there mode 0600, and so is any other file the preset asks for, through `runner.write_private`, which refuses paths outside the directory. The directory is removed when the run ends, and leftovers are removed when the runner starts.
- **The environment** is an allowlist: a few base variables, the names `JOTFLOW_AGENT_ENV_ALLOW` lists, and the preset's own extras. A preset's extras can't override a base, allowlisted, or runner-set name, nor any JotFlow, Slack, or GitHub name.
- **One GitHub token per container.** The `agent` container holds only the read-only token and the `agent-exec` container only the write token. A drafting run can't reach the write token even with a shell, because it isn't in that container.
- **Per-run uids, when the team turns them on (#181).** With `JOTFLOW_RUNNER_ISOLATION=uid`, the run's agent runs as its own uid, with `HOME` and `TMPDIR` inside its run directory and `/tmp` closed. A preset must not rely on the service's home, a CLI login stored there, or writing outside `HOME`, `TMPDIR` and its run directory, and it must take its login from an API key in the environment.
- **Limits.** Each run has a time limit, after which its whole process group is killed. The agent's stdout and stderr are discarded, because results come only through JotFlow's MCP tools.
- **Tokens.** A GitHub token is refused at startup, and a run token when a run is launched, if it holds characters a CLI's `${VAR}` expansion could act on.

## Writing a preset

A preset is a small frozen dataclass with a `plan()` method. The runner calls `plan()` once per run.

```python
@dataclass(frozen=True)
class ExamplePreset:
    name: str = "example"
    default_models: Mapping[RunKind, str] = field(default_factory=dict)
    needs_github_mcp: bool = True  # the runner checks the binary at startup
    github_token_in_config: bool = True  # the runner checks the token's characters

    def plan(self, run: RunContext) -> RunPlan:
        allowed = [*JOTFLOW_TOOLS[run.kind], *GITHUB_TOOLS[run.kind]]
        config = {
            "mcpServers": {
                "jotflow": jotflow_server(run.mcp_url, run.mcp_token),
                "github": github_server(run),  # --read-only and --tools for the kind
            }
        }
        argv = (
            "example-cli",
            "run",
            "--mcp-config",
            str(run.config_path),
            "--allow-tools",
            ",".join(allowed),
            PROMPTS[run.kind],
        )
        return RunPlan(
            argv=argv,
            config=config,
            env={"EXAMPLE_HOME": str(run.run_dir / "home")},
            github_token_env=False,
        )
```

Then add it to `PRESETS`, and to `EXPERIMENTAL` until the maintainer drops the label (step 5).

**`RunContext`** is everything a preset may use:
- the run kind and run id
- the run's private directory and its `config_path`
- JotFlow's MCP URL and the run token
- the one GitHub token for this kind
- the model
- the path of the pinned GitHub MCP server

**`RunPlan`** is what the runner starts:
- **`argv`**
- **`config`**, the JSON written to `config_path`
- **`env`**, extras on top of the allowlist
- **`github_token_env`**, which says whether `GITHUB_TOKEN` and `GH_TOKEN` are set. Keep it false when the token is in the config.
- **`files`**, more private files by path inside the run directory, such as a CLI's own config file. See the Codex preset's `codex/config.toml`. Codex is a drafting and question preset only, and it's refused for execution.

Two optional members, which the runner looks for:
- **`check_install()`** returns why the installed CLI can't honour the preset, or None. The runner calls it at startup and refuses to start on a problem: a different version, a missing feature flag, or a missing binary.
- **`run_kinds`** is the run kinds the preset may serve. A runner serving any other kind refuses to start. The codex preset serves drafting and question only, because Codex can't drop its MCP resource-read tools, and an execution run must never have them.

Rules for the code:
- **The prompt** is a fixed string per run kind. It tells the agent to call `get_work` first and follow its guidance. JotFlow's instructions arrive in the work packet, the same for every CLI. Never put run data in the prompt or anywhere in `argv`: no token, run id, URL, path, or model, nor anything from Slack.
- **Exactly two MCP servers:** JotFlow and the pinned GitHub server (`github_server(run)`). Load no others, and turn off the CLI's own MCP discovery (Claude Code's `--strict-mcp-config`, for example).
- **Tools per run kind.** Limit each server to `JOTFLOW_TOOLS[kind]` and `GITHUB_TOOLS[kind]`, in the CLI's own allowlist as well as on the server. Drafting and question runs start the GitHub server with `--read-only`, which wins over its tool list.
- **No built-in tools** that run code, touch files, or reach the network: no shell, exec, code interpreter, file write, web fetch, browser, sub-agents, or plugins. Turn each one off. If the CLI can't turn one off, the preset must make it harmless, as a read-only sandbox does for a patch tool, and prove that.
- **No resource or file-read tools on execution runs.** An execution run holds the write token and must see only the approved fields. Tools such as `read_mcp_resource` or `list_mcp_resource_templates` would let it read repository contents and bring untrusted text into a write run. If the CLI can't drop them, set `run_kinds` to drafting and question only. The runner then refuses the preset for an execution runner, as it does for codex.
- **Nothing kept outside the run.** Point the CLI's home, cache, logs, session transcripts, and memory into the run directory, so they're removed with it, or turn them off. Turn off telemetry and self-updates where the CLI allows.
- **Nothing prompts.** A headless run has nobody to answer. Configure the CLI so anything not allowed is denied, never asked about.
- **Pin the CLI** to an exact version in `AGENT_NPM_PACKAGES`, or a checksum for a binary. Check that version in `check_install()`.

## What a preset must guarantee

| Guarantee | Why | How it's proven |
|---|---|---|
| The model is offered exactly the expected tools for each run kind, and no shell or exec | Slack text is untrusted, and a shell could read `/proc/1/environ`, which holds the runner secret and the container's GitHub token ([threat model: a prompt-injected drafting agent](threat-model.md#a-prompt-injected-drafting-agent)) | The tool-set gate, below, asserts the exact offered set from the recorded request |
| A tool that writes files, if the CLI insists on one, is refused | The run directory holds the private config | The gate scripts a call to it and asserts it's refused and wrote nothing |
| Only JotFlow and the pinned GitHub server are loaded, each limited to the kind's tools | Tool shadowing and rug pulls come from other servers | Unit tests pin the config. The gate shows the servers the CLI connected to. |
| Drafting has read-only GitHub, and only execution can write | D6, and the split tokens | Unit tests on `--read-only` and the tool lists, plus the container split |
| No resource-read tools on execution: an execution run sees only the approved fields, and can't pull repository text into its context | It holds the write token, and arbitrary repo text, such as an attacker's README, could steer it ([threat model: a prompt-injected drafting agent](threat-model.md#a-prompt-injected-drafting-agent)) | The gate's exact tool set for execution. If the CLI can't drop such tools, set `run_kinds` so the preset never serves execution, as codex does: the runner refuses the preset for `kinds={execution}`, proven by `test_an_execution_runner_set_to_codex_refuses_to_start` |
| No token in `argv` or the CLI's environment | `ps`, logs, and transcripts | Unit tests on `argv` and `env`, plus the runner's end-to-end test with a fake CLI binary |
| No run data in the prompt | The prompt is the same for everyone. Run data comes through `get_work`. | A test like `test_no_prompt_carries_run_data` |
| The CLI's state stays in the run directory, or is off | Nothing outlives the run | An end-to-end test that the run directory, and so the state, is gone afterwards |
| Nothing prompts, and nothing waits | A headless run would hang until its time limit | The gate asserts the CLI ended by itself |
| Fail closed on an unverified CLI version, feature, or model | A new release can change the tool set | `check_install()`, and a startup probe of the offered tools where they depend on the model, as `adapters/codex_probe.py` does for Codex, with tests for each refusal |

## Proving it

**1. Unit tests**, in `tests/test_presets.py`. Pin the whole `argv` and the whole generated config for each run kind. Show that the prompt holds no run data, that the env holds no token, and that `check_install()` and model refusals work. Add an end-to-end test that runs the real runner with a fake CLI binary on `PATH`, which records what it was given (see `test_a_codex_run_end_to_end`).

**2. The tool-set gate**, against the real CLI, at no cost. Point the CLI at a scripted fake model API on 127.0.0.1 instead of the vendor's. `jotflow.testing.fake_responses_api` speaks the OpenAI Responses API. A CLI on another API needs an equivalent fake. Run the CLI with the preset's own generated config, overriding only the model provider, and assert three things:
- the recorded first request offers exactly the expected tools, and nothing shell-like
- a scripted call to any write-capable built-in is refused, and no file appears
- the CLI exits by itself, and JotFlow's audit log shows it reached the MCP server

`tests/test_codex_gate.py` is the worked example. It covers drafting and question only, because the codex preset's `run_kinds` excludes execution, and `test_an_execution_runner_set_to_codex_refuses_to_start` shows the refusal. Run it in CI with the CLI installed from a lockfile, and with an environment flag, such as `JOTFLOW_REQUIRE_CODEX=1`, that makes a missing CLI fail rather than skip (see the "Codex preset gate" job).

**3. The whole path, without a model:**

```
jotflow replay --all --agent scripted
jotflow replay --all --agent scripted --execute
```

The stand-ins go through the real runner, run tokens, and MCP server, with the fake GitHub server. These check the harness and the fixtures, not your CLI.

**4. A real pass,** which costs model tokens and needs the maintainer's approval:

```
jotflow replay --all --agent <preset> --repeat 3 --yes
jotflow replay --all --agent <preset> --execute --yes
```

Both print the run count and the model before they start, and refuse without `--yes`. Drafting is compared with each fixture's expected outcome. Execution is compared exactly with the writes JotFlow planned, and again after a scripted interrupted attempt, which must find the marker and write nothing. Record the pass counts and token totals in the preset's plan and in presets.md. Keep counts only, never transcripts or fixture text.

**5. Drop the experimental label** when the maintainer decides to. The gate must pass in CI first, and usually a real pass of every kind the preset serves is recorded too. Until then, keep the preset in `EXPERIMENTAL`, so the runner's startup line says "(experimental)". Either way, presets.md says what's verified and what isn't. Codex dropped the label on 2026-10-19, by the maintainer's decision, with its gate passing and no real pass recorded yet.

## A checklist for a new preset's PR

- [ ] The preset is in `PRESETS`, and in `EXPERIMENTAL` until step 5, and the CLI is pinned to an exact version
- [ ] `argv` and the config are pinned per run kind in tests, and no run data is in the prompt or `argv`
- [ ] Only JotFlow and the GitHub server are loaded, each limited to the kind's tools, with GitHub `--read-only` for conversation runs
- [ ] Execution runs have no resource-read or other context-pulling tools, or the preset's `run_kinds` excludes execution, with a test that an execution runner set to it refuses to start (as for codex, which is drafting and question only)
- [ ] No token is in `argv` or the CLI's environment
- [ ] The CLI's home, cache, logs, and transcripts are in the run directory, or off, and an end-to-end test shows them removed
- [ ] The gate runs in CI: the exact tool set, no shell or exec, write tools refused, and no prompt
- [ ] `check_install()` fails closed on version and flags, and the runner probes the offered tools at startup if they depend on the model
- [ ] `jotflow replay --agent scripted` and `--execute` pass
- [ ] presets.md has the preset's section: command, config, login, what was checked, and what's unconfirmed
- [ ] A real pass is recorded, or presets.md says none is, and the preset stays experimental unless the maintainer has dropped the label

## Upgrading a pinned CLI

A new release can change the tool set, the flags, or the defaults. Change the pin in `AGENT_NPM_PACKAGES`, the version `check_install()` expects, and the gate's lockfile together. Then run the unit tests and the gate. A new built-in tool fails the gate's exact-set assertion, which is the point. Run a real pass if the change could affect behaviour, and update the table in presets.md with the new version and date.
