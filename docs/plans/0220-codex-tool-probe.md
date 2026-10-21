# A startup tool-set probe for Codex, in place of its model allowlist

## Links and status

- Issue: #220 (M6)
- Also serves: #274, which the same evidence unblocks
- Draft PR: #283
- Status: planning. Decisions 1 to 4 below need the lead before any code
- Last verified: origin/main at the time of writing, 2026-10-21, with Codex 0.163.0 and github-mcp-server 2.0.2, both run offline in a network namespace with only loopback
- Next safe action: the lead decides 1 to 4; security reviews the findings

## Objective and done-when

The codex preset accepts only `gpt-5.5`. That list stands in for the real rule: never start a codex run whose offered tool set holds a shell or code-execution tool. This plan enforces the rule in code, so any model whose tool set is exactly the safe set is accepted, and drops the list.

From #220:

- [ ] The spike settles whether Codex's per-model tool set can change after startup, from remote metadata, and how the probe covers that. **Settled below, and the answer is yes.**
- [ ] Any model whose probed tool set is exactly the safe set is accepted. Any model that adds a tool is refused, with the tool named. Tested with the fake API.
- [ ] `CODEX_VERIFIED_MODELS` is removed, and presets.md and the threat model describe the probe.
- [ ] Security reviews it.

From #274, if decision 4 folds it in:

- [ ] The GitHub MCP server, started as the preset starts it, serves no repository resources, and the probe proves it.
- [ ] `docs_conflict` and its tests are removed, and presets.md, install.md and the threat model are updated.

**Out of scope:** Codex execution runs, which stay refused. Real-model runs, which need approval.

## Spike findings

All of this was run offline with the pinned binaries: `unshare -rn`, with only loopback up. One early probe, before the namespace was set up, reached `api.openai.com` with a dummy key and got a 401. No model ran and nothing was spent.

1. **Each model's tool set comes from catalog metadata, not from config.**
   - In Codex's bundled `models.json`, `gpt-5.5` has `"tool_mode": null`. Every newer model has `"tool_mode": "code_mode_only"`, and most have `"multi_agent_version": "v2"`.
   - A local `model_catalog_json` that sets `gpt-5.5` to `code_mode_only` makes Codex offer `exec`, `collaboration` and `wait`. That holds with every related feature off, `code_mode_host` and `multi_agent` included.
2. **With an API key and the built-in OpenAI provider, which is how a deployment runs, Codex fetches the remote model list on every run.**
   - Its log shows `list_models{refresh_strategy=online_if_uncached}`, then "no usable cache entry", then an online refresh.
   - Each run has a fresh `CODEX_HOME`, so there is never a cache.
   - So a remote catalog update could switch `gpt-5.5` into code mode today, with no change on our side. **That is a live gap in the current preset**, and presets.md's "Not yet confirmed" list already names it.
3. **`features.api_key_model_discovery = false` stops the remote fetch entirely.** No `list_models` refresh appears in the log, and Codex uses its bundled catalog. A custom provider, such as the one the CI gate and the probe use, never fetches `/models` at all. That held even with `requires_openai_model_catalog_url` or `requires_openai_auth` set.
4. **Codex 0.163.0 has no switch for its MCP resource tools.**
   - `list_mcp_resources`, `list_mcp_resource_templates` and `read_mcp_resource` appear whenever any MCP server is configured, whatever capabilities that server declares.
   - The `[tools]` table holds only `web_search`, `experimental_request_user_input` and `update_plan`.
   - `model_messages.tools.mcp_resources` is the tools' description text, not an on/off switch.
5. **github-mcp-server 2.0.2, started as the preset starts it, serves no repository content.** That's `stdio --read-only --tools <the kind's tools>`.
   - `resources/templates/list` is empty, and `resources/list` holds one static UI page.
   - `resources/read` of `repo://acme-co/web/contents/README.md` answers "Resource not found" at once, with no network attempt. That holds even with `get_file_contents` in `--tools`.
   - With its default toolsets, the same server registers five `repo://` templates and tries to fetch the file.
   - So Codex's resource tools exist, but under the preset nothing behind them can read a repo file. The `--tools` list is the "flag that serves no repository resources" #274 asks for. It isn't a promise of the server's, so the probe must check it every time.

## Design

### The preset

- **The fix:** `features.api_key_model_discovery = false` in every codex run's config, and in the probe's. That closes finding 2.
- **`shell_snapshot` joins `CODEX_FEATURES_OFF`.** It is on by default and makes Codex run the login shell once to capture its environment. That isn't an agent tool, but nothing needs it.
- **`CODEX_VERIFIED_MODELS` is removed.** The default model stays `gpt-5.5`, as `CODEX_DEFAULT_MODEL`. setup and `jotflow check` stop refusing other model names, and say the runner checks the model's tools at startup instead.

### The startup probe (src/jotflow/adapters/codex_probe.py, new)

`RunnerConfig` validation calls it when the preset is codex. It runs once for each distinct model the runner is configured with, a drafting model and a question model.

**What it runs:** the preset's own `plan()` for a probe run context. It changes only:

- the model provider, routed to an in-process fake Responses API, the same code as `testing/fake_responses_api.py`, moved to a non-test module;
- the JotFlow server, which is left out. A probe has no run token, and resource tools are added by any server, so the GitHub server is enough;
- the GitHub token, a dummy. It's never sent, because no tool is called.

The GitHub server is the real pinned binary with the preset's arguments. Codex then runs one turn, and the probe reads the first request it sends.

**What it checks, as exact sets:**

- The top-level tools are exactly `{apply_patch, tool_search, list_mcp_resources, list_mcp_resource_templates, read_mcp_resource}`, plus the run kind's own MCP tools.
- There is no `additional_tools` namespace at all. That's where `exec`, `spawn_agent` and the other sub-agent tools appear.
- The model in the request is the configured one.
- **For #274, the server side:** the probe also talks to the GitHub server directly, over stdio. `resources/templates/list` must be empty. Every `resources/list` entry must be a `ui://` URI. A `resources/read` of a `repo://` URI must fail with an error and no network attempt, which the probe sees as an answer within a second.

**Failing closed:** the runner refuses to start, naming the problem, in all of these cases:

- any extra tool or namespace, with the extras named;
- a missing expected tool, since the set must be exact;
- a Codex exit that isn't clean, or no request within 30 seconds;
- output that won't parse;
- a server-side resource check that doesn't hold.

The reason goes in the runner's log and in `/info`, as other startup refusals do.

**When it runs:** at runner startup only, not before each run. With discovery off, the tool set depends only on the bundled catalog, the config and the CLI version. The CLI version is already pinned at startup by `check_install`, and the config is JotFlow's own. So nothing can change it between startup and a run. A per-run probe would add a Codex launch, a few seconds, to every run for no gain.

### CI

- The existing codex gate keeps its default-model test.
- It gains the probe's own test against the real binaries: `gpt-5.5` passes, and a code-mode model from the bundled catalog, such as `gpt-6.1-sol`, is refused with `exec` named.
- It gains an offline check that the built-in provider makes no `/models` request with discovery off. It runs under `unshare -rn` where the runner allows user namespaces, which GitHub's Ubuntu runners do, and is skipped with a reason elsewhere.

### Docs

- **presets.md:** the probe replaces "Limit: other models are refused". The "Not yet confirmed" item on remote metadata becomes a confirmed finding, with the fix.
- **Threat model:** the codex accepted-gap row describes the probe and the discovery fix. If #274 is folded in, it also says resource tools reach nothing under the preset's GitHub server, as the probe checks.
- **install.md and the CHANGELOG.**

## Decisions for the lead

1. **Ship the discovery fix now, on its own.** It's one config line and a gate test. It closes a live gap: today a remote catalog update could put the codex preset into code mode. I'd send it as a small PR ahead of the rest, with security's review. The alternative is to wait for #220's PR.
2. **Probe at startup only, not per run.** That's my recommendation, for the reason under "When it runs". The cost of being wrong is a Codex CLI that changes its tools without a version change, which the version pin rules out.
3. **The probe's provider differs from a run's.** It must route Codex to a fake API, so it uses a custom provider, while runs use the built-in OpenAI one. With discovery off, both read the bundled catalog. But a provider-specific built-in tool could differ: `web_search` is one, and it's disabled. The CI gate shows today's set matches. I'd accept this residual difference and name it in the threat model. The alternative is no probe, keeping the allowlist.
4. **#274 in the same PR, or right after.** The evidence is the same, and the server-side check is about 40 lines. I'd fold it in, so one security review covers both. The alternative is a follow-up PR.

## Risks and open questions

- **A newer github-mcp-server could register `repo://` templates under `--tools`.** The probe refuses to start the runner then, so it fails closed, which turns codex drafting off. The image pins the server version.
- **Probe cost:** one Codex launch per distinct model at runner startup, about 3 seconds each. That's acceptable.
- **The fake API moves out of `testing/`,** since the runner image needs it. It holds no secrets and serves only on 127.0.0.1.

## Progress checklist

- [x] Confirm the issue is ready, by running the spike
- [x] Commit this plan and open the draft PR
- [ ] The lead decides 1 to 4, and security reviews the findings
- [ ] (Decision 1) The discovery fix in its own PR
- [ ] The probe module, the runner wiring, and removal of the allowlist
- [ ] (Decision 4) The server-side resource check and lifting `docs_conflict`
- [ ] CI gate tests
- [ ] Docs
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

The spike scripts and their output are summarised under Spike findings. They ran from the job's scratch space against Codex 0.163.0, installed from npm, and github-mcp-server 2.0.2, from its release tarball, under `unshare -rn`.

## Handoff notes

- No model spend. The one stray request was a 401 with a dummy key, before the network namespace was in place.
