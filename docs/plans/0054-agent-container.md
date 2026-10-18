# Agent container and runner

## Links and status

- Issue: #54
- Also serves: #21, which calls this runner from JotFlow's side
- Draft PR: pending
- Status: planning
- Last verified: plan written on 2026-10-18, nothing built yet
- Next safe action: commit this plan, push, open the draft PR

## Objective and done-when

Run the team's agent in its own container (D6). The container sees its own model login and GitHub tokens, never JotFlow's `.env`, Slack tokens, or database. A small runner inside it starts each run when JotFlow asks, with exactly the access that run's kind allows.

The issue's boxes:

- [ ] `docker compose up` starts both containers
- [ ] A test proves Slack tokens are not visible inside the agent container
- [ ] JotFlow can launch a run in the agent container, and the agent reaches the MCP port
- [ ] Docs explain how to provide the agent's model login and GitHub tokens

From the dated notes on #54 and #21:

- [ ] The runner listens only on the internal `agents` network, with no published port and no Docker socket
- [ ] The runner accepts exactly run id, run kind, and per-run token, never a command. It authenticates JotFlow with a shared secret that is compared in constant time and never given to the agent.
- [ ] The read-only GitHub token goes to drafting and question runs, and the write token only to execution runs. A missing token refuses the run, and drafting never falls back to the write token.
- [ ] Each run gets a private temporary directory (0700) holding its MCP config (0600), with the MCP URL and the token as an `Authorization` header. The directory is deleted when the run ends, including on timeout, kill, or a runner restart.
- [ ] The agent process gets an explicit environment allowlist: base variables, the model-login variables the team names, and the one GitHub token for its kind. Nothing else, so not the runner secret either.
- [ ] A run past its time limit is killed with its whole process group
- [ ] JotFlow's MCP server listens on the internal network inside Compose, with `JOTFLOW_MCP_HOST=0.0.0.0` and `JOTFLOW_MCP_ALLOWED_HOSTS=jotflow`

**Split with #21.** This PR is the agent side: the container, the runner, and how a run's process is started, scoped, and ended. #21 is the JotFlow side: it asks the runner to start a run, follows the run to its end, finishes it through `RunLifecycle`, and posts a short notice in Slack on failure.

**Out of scope:** the agent CLIs and their presets and prompts (#22, #31), with the image taking the CLI's npm package as a build argument; the JotFlow-side client (#21); the end-to-end issue (#55); writing the runner secret from `jotflow init`, which is a follow-up for #7, while this PR documents generating it by hand.

## Design

See Deployment, How runs start, Network, and GitHub tokens in [architecture.md](../architecture.md), and D6 and D13 in [roadmap.md](../roadmap.md).

**`jotflow runner`**, a new subcommand in `commands/runner.py` with its logic in `src/jotflow/agent/runner.py`. It is a small Starlette app on uvicorn, port 8766, with the same kind of gate as the MCP server:

- refuse any `Origin`, any `Host` off the allowlist (`agent:8766` and loopback), any method other than the three below, any query string, and any body over 4 KiB
- `Authorization: Bearer <runner secret>`, compared with `hmac.compare_digest`
- `POST /runs` with `{run_id, kind, token}`. The body is strict: a run id pattern, a known kind, and a token starting `jfr_`. The reply is 202 with the run's state. A second POST for a live run is 409.
- `GET /runs/{id}` returns `{state, exit_code}`, where state is starting, running, exited, timed_out, or killed
- `DELETE /runs/{id}` kills the run, for JotFlow's shutdown (#55's note) and for its timeouts
- at most a fixed number of live runs; the cap matches JotFlow's

**Launching a run:**

1. `tempfile.mkdtemp()` gives a private directory under the container's `/tmp` tmpfs.
2. `mcp.json` is written there with `os.open(..., 0o600)`. It holds the server URL and `Authorization: Bearer <run token>` in the shape agent CLIs read.
3. The child environment is the allowlist:
   - `PATH`, `LANG`, `TZ`, and `HOME`, set to the run's directory so CLIs write their state there
   - each name listed in `JOTFLOW_AGENT_ENV_ALLOW`, such as the model API key
   - `GITHUB_TOKEN` and `GH_TOKEN`, from `JOTFLOW_GITHUB_READ_TOKEN` or `JOTFLOW_GITHUB_WRITE_TOKEN` by kind
   - `JOTFLOW_RUN_ID` and `JOTFLOW_MCP_CONFIG`
4. The command is argv from `JOTFLOW_AGENT_COMMAND`, a JSON list with `{mcp_config}` and `{run_id}` placeholders, filled in with no shell. #22's presets replace this with named presets. The token never appears in argv.
5. The process starts in its own session. On the time limit or a DELETE, its whole process group gets SIGTERM, then SIGKILL after a grace period.
6. The directory is removed in a `finally`, and every leftover directory is removed when the runner starts.

**Image, `Dockerfile.agent`:** the same pinned Python base as JotFlow's, plus Debian's `nodejs` and `npm`. Agent CLIs install through `ARG AGENT_NPM_PACKAGES`, pinned by the preset. JotFlow is installed from `uv.lock` for the runner. The image runs as user `agent` (uid 10002) with a read-only root, `/tmp` as tmpfs, and `/home/agent` as a named volume for the CLI's model login. All capabilities are dropped, with `no-new-privileges`.

**Compose:**

- The `agent` service builds `Dockerfile.agent` and reads `agent.env`, never `.env`. It joins `agents` and a new `agent-egress` network, for the model API and GitHub. It publishes no ports.
- `jotflow` gets `JOTFLOW_MCP_HOST=0.0.0.0`, `JOTFLOW_MCP_ALLOWED_HOSTS=jotflow`, and `JOTFLOW_RUNNER_URL=http://agent:8766`.
- The runner secret lives in both `.env` and `agent.env`, as `JOTFLOW_RUNNER_SECRET`.

**Docs:** an "Agent container" part of Deployment. It covers the two env files, generating the runner secret, the model login (an API key in `agent.env`, or a one-time interactive login into the `agent-home` volume), and two fine-grained GitHub tokens: read-only for contents and issues, and write for issues only on the channel's repos.

Alternatives considered:

- `docker exec` from JotFlow: needs the Docker socket, which D6 forbids.
- A runner in Node: the runner shares JotFlow's models and tests in Python.
- Passing the token in the environment: CLIs and their tools can print the environment. A 0600 file in a private directory reaches only the agent's own process.

## Risks and open questions

- Debian's `nodejs` version must satisfy the CLIs. Claude Code needs Node 18 or newer, and trixie ships 20. #22 pins the versions.
- A CI test runs the image and proves the agent process's environment and the container's own environment hold no Slack variable. Reaching the MCP port across the two containers needs JotFlow connected to Slack, so CI proves it in process with the real MCP app and a stub agent. The Compose-level check is a live step for #55.
- The runner secret is one more value to set up by hand until `jotflow init` writes it (#7 follow-up).

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Runner app: gate, routes, run table, cap
- [ ] Launch: private directory, 0600 config, environment allowlist, token by kind, argv template, process group kill, cleanup
- [ ] `jotflow runner` subcommand and settings
- [ ] `Dockerfile.agent`, Compose service and networks, JotFlow's MCP env in Compose
- [ ] Tests: gate, body, secret, token by kind, environment, file modes, cleanup, timeout kill, a stub agent reaching the real MCP app
- [ ] CI: build the agent image, prove no Slack variable inside it
- [ ] Update documentation: Deployment, README setup for `agent.env`
- [ ] `make check` passes
- [ ] CodeRabbit local review, or skipped
- [ ] `closingIssuesReferences` checked, then mark the PR ready

## Validation

To be filled in.

## Handoff notes

None yet.
