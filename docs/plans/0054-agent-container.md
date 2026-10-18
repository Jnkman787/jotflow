# Agent container and runner

## Links and status

- Issue: #54
- Also serves: #21, which calls this runner from JotFlow's side
- Draft PR: #123
- Status: ready for review
- Last verified: `make check` green, 1281 tests, 98.3% coverage; the agent image was built and smoke-tested locally
- Next safe action: reviewer reviews #123; security owns it, so reviewer may route it back for a look

## Objective and done-when

Run the team's agent in its own container (D6). The container sees its own model login and GitHub tokens, never JotFlow's `.env`, Slack tokens, or database. A small runner inside it starts each run when JotFlow asks, with exactly the access that run's kind allows.

The issue's boxes:

- [x] `docker compose up` starts both containers, once `COMPOSE_PROFILES=agent` is set; see Design
- [x] A test proves Slack tokens are not visible inside the agent container
- [ ] JotFlow can launch a run in the agent container, and the agent reaches the MCP port. In process this is proven, with the runner launching a stub agent that calls the real MCP server with its token. JotFlow's side of the call is #21, and the cross-container live step is #55.
- [x] Docs explain how to provide the agent's model login and GitHub tokens

From the dated notes on #54 and #21:

- [x] The runner listens only on the internal `agents` network, with no published port and no Docker socket
- [x] The runner accepts exactly run id, run kind, and per-run token, never a command. It authenticates JotFlow with a shared secret that is compared in constant time and never given to the agent.
- [x] The read-only GitHub token goes to drafting and question runs, and the write token only to execution runs. After review, they live in two separate services, `agent` and `agent-exec`, so the write token never exists in a container a drafting run can reach. Each runner refuses kinds it does not serve, and refuses to start holding the other kind's token. Execution runs go one at a time.
- [x] Each run gets a private temporary directory (0700) holding its MCP config (0600), with the MCP URL and the token as an `Authorization` header. The directory is deleted when the run ends, including on timeout, kill, or a runner restart.
- [x] The agent process gets an explicit environment allowlist: base variables, the model-login variables the team names, and the one GitHub token for its kind. Nothing else, so not the runner secret either.
- [x] A run past its time limit is killed with its whole process group
- [x] JotFlow's MCP server listens on the internal network inside Compose, with `JOTFLOW_MCP_HOST=0.0.0.0` and `JOTFLOW_MCP_ALLOWED_HOSTS=jotflow`

**Split with #21.** This PR is the agent side: the container, the runner, and how a run's process is started, scoped, and ended. #21 is the JotFlow side: it asks the runner to start a run, follows the run to its end, finishes it through `RunLifecycle`, and posts a short notice in Slack on failure.

**Out of scope:** the agent CLIs and their presets and prompts (#22, #31), with the image taking the CLI's npm package as a build argument; the JotFlow-side client (#21); the end-to-end issue (#55); writing the runner secret from `jotflow init`, which is #126, while this PR documents generating it by hand.

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
   - `PATH` and locale variables, `HOME` (the service's own login volume, so the CLI finds its model login), and `TMPDIR` (the run's directory)
   - each name listed in `JOTFLOW_AGENT_ENV_ALLOW`, such as the model API key
   - `GITHUB_TOKEN` and `GH_TOKEN`, from `JOTFLOW_GITHUB_READ_TOKEN` or `JOTFLOW_GITHUB_WRITE_TOKEN` by kind
   - `JOTFLOW_RUN_ID` and `JOTFLOW_MCP_CONFIG`
4. The command is argv from `JOTFLOW_AGENT_COMMAND`, a JSON list with `{mcp_config}` and `{run_id}` placeholders, filled in with no shell. #22's presets replace this with named presets. The token never appears in argv.
5. The process starts in its own session. On the time limit or a DELETE, its whole process group gets SIGTERM, then SIGKILL after a grace period.
6. The directory is removed in a `finally`, and every leftover directory is removed when the runner starts.

**Image, `Dockerfile.agent`:** the same pinned Python base as JotFlow's, plus Debian's `nodejs` and `npm`. Agent CLIs install through `ARG AGENT_NPM_PACKAGES`, pinned by the preset. JotFlow is installed from `uv.lock` for the runner. The image runs as user `agent` (uid 10002) with a read-only root, `/tmp` as tmpfs, and `/home/agent` as a named volume for the CLI's model login. All capabilities are dropped, with `no-new-privileges`.

**Review change: two agent services.** The reviewer showed that runs sharing a uid can read each other's `/proc/<pid>/environ` and run directories, Yama `ptrace_scope=1` notwithstanding. The lead added that the runner holds its env file in its own environment, readable at `/proc/1/environ`. So in one container, a drafting run with only a file-read tool could take the write token. The fix is one image, two services:

- `agent` serves drafting and question runs with `agent.env`: the read token and the model login
- `agent-exec` serves execution runs only, one at a time, with `agent-exec.env`: the write token and the model login

Each has its own home volume, which also stops a drafting run from planting CLI config that an execution run would load. A second review found the same problem one level up: both runners shared one secret and one network. A drafting agent could then read its runner's secret from `/proc/1/environ` and start a write-token process on `agent-exec`. So each runner now has its own secret. JotFlow's `.env` holds `JOTFLOW_RUNNER_SECRET` for `agent` and `JOTFLOW_EXEC_RUNNER_SECRET` for `agent-exec`, and sends each only to its runner. Each service also has its own internal network to JotFlow, `agents` or `agents-exec`, and its own egress, so the drafting container cannot reach `agent-exec` at all. Within one service, runs can read each other's environment and run directory. That is accepted, because they hold the same token. `RunnerConfig` enforces the split: its kinds are either drafting and question or execution only, it holds exactly that side's token, and `JOTFLOW_RUNNER_KINDS` selects which.

**Compose:**

- The agent service is behind the profile `agent`. Without `agent.env` the runner can only fail, and its restart policy would loop, so plain `docker compose up -d` keeps starting JotFlow alone until a team adds `COMPOSE_PROFILES=agent` to `.env`. That is how both containers start, and it keeps today's setup unchanged.

- The `agent` and `agent-exec` services share the `Dockerfile.agent` image. Each reads its own env file, never `.env`, and has its own home volume, `agent-home` or `agent-exec-home`. Both join `agents` and a new `agent-egress` network, for the model API and GitHub, and publish no ports.
- `jotflow` gets `JOTFLOW_MCP_HOST=0.0.0.0` and `JOTFLOW_MCP_ALLOWED_HOSTS=jotflow`. #21 adds `JOTFLOW_RUNNER_URL=http://agent:8766` along with the client that reads it.
- The runner secret lives in both `.env` and `agent.env`, as `JOTFLOW_RUNNER_SECRET`.

**Docs:** an "Agent container" part of Deployment. It covers the two env files, generating the runner secret, the model login (an API key in `agent.env`, or a one-time interactive login into the `agent-home` volume), and two fine-grained GitHub tokens: read-only for contents and issues, and write for issues only on the channel's repos.

Alternatives considered:

- `docker exec` from JotFlow: needs the Docker socket, which D6 forbids.
- A runner in Node: the runner shares JotFlow's models and tests in Python.
- Passing the token in the environment: CLIs and their tools can print the environment. A 0600 file in a private directory keeps it out of argv, env, and anything the CLI prints. It does not hide it from other runs in the same service, which share the uid; the two services are what keep the write token away from drafting runs.

## Risks and open questions

- Debian's `nodejs` version must satisfy the CLIs. Claude Code needs Node 18 or newer, and trixie ships 20. #22 pins the versions.
- A CI test runs the image and proves the agent process's environment and the container's own environment hold no Slack variable. Reaching the MCP port across the two containers needs JotFlow connected to Slack, so CI proves it in process with the real MCP app and a stub agent. The Compose-level check is a live step for #55.
- The runner secret is one more value to set up by hand until `jotflow init` writes it (#126).

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Runner app: gate, routes, run table, cap
- [x] Launch: private directory, 0600 config, environment allowlist, token by kind, argv template, process group kill, cleanup
- [x] `jotflow runner` subcommand and settings
- [x] `Dockerfile.agent`, Compose service and networks, JotFlow's MCP env in Compose
- [x] Tests: gate, body, secret, token by kind, environment, file modes, cleanup, timeout kill, a stub agent reaching the real MCP app
- [x] CI: build the agent image, prove no Slack variable inside it
- [x] Update documentation: Deployment, README setup for `agent.env`
- [x] `make check` passes
- [x] CodeRabbit local review, or skipped: skipped, rate limit
- [x] `closingIssuesReferences` checked, then mark the PR ready

## Validation

- Second review change: distinct runner secrets and separate networks. A CI step starts both agent services and shows `agent` cannot open a connection to `agent-exec:8766`. As a positive control, a container on `agents-exec` can. CI asserts the two runners' secrets differ, that the agent services share no network while JotFlow is on both, and that `agent-exec` answers 401 to the drafting runner's secret and 403 to a drafting run. Both passed locally.
- Two-service review change: 58 runner tests cover a conversation runner refusing the write token, an execution runner refusing the read token, mixed kinds refused, each runner refusing the other's kinds with 403, one execution run at a time, and a chunked body capped while it is read. CI's Compose policy asserts no ports, no host network, no privileged mode, and no docker.sock on any service. It also asserts that each agent service's environment is exactly its own env file, so `agent` has no write token and `agent-exec` no read token, with no bind mounts and separate homes. A CI step starts `agent-exec` and shows it refuses a drafting run with 403 and cannot see the `.env` sentinel. Both passed locally.
- `make check` passes: 1281 tests, 98.3% total coverage. `adapters/runner.py` is at 96%. `commands/runner.py`'s uvicorn loop is not covered; the CI job starts the real runner.
- `tests/test_runner.py`, 47 tests:
  - configuration: a short secret, an empty command, a non-HTTP MCP URL, a bad env name, and allowlisting any JotFlow, Slack, GitHub, or `*_TOKEN` name all fail at startup. The config's `repr` hides its secrets. The command must be a JSON list of strings, and only `{mcp_config}` and `{run_id}` are filled.
  - the launch request is exactly run id, kind, and a `jfr_` token. An extra field, a spaced run id, an unknown kind, a non-JotFlow token, and a spaced token are refused. The run-id pattern is anchored, because Pydantic's `pattern` searches rather than matches.
  - the child environment holds exactly the allowlist, with the read token for drafting and question runs and the write token for execution. Slack sentinels, the runner secret, the other GitHub token, and unlisted variables are absent.
  - real launches with a stand-in agent: the MCP config is 0600 in a 0700 directory, the agent runs in that directory, and the token is in neither argv nor env. The directory is gone afterwards.
  - exit codes are reported. A missing token refuses the run without fallback. One launch per run id, and the live-run cap holds.
  - a timeout kills the whole process group, grandchild included. DELETE kills the run. An agent that cannot start fails cleanly. Leftover directories are removed at start.
  - the HTTP gate: an `Origin`, a bad `Host`, a query, and a missing or wrong secret are refused before anything starts, and an oversized body is refused. Launch, status, and stop work over HTTP.
  - a launched stand-in agent reads its MCP config and calls JotFlow's real MCP server on loopback with its issued token: HTTP 200, with tools-only capabilities.
  - the subcommand and its settings, and configuration problems exit 2 naming the variable
- Local Docker, Docker 28.3: the agent image builds with Node 20 and runs as uid 10002. With Slack sentinels in `.env`, neither `docker compose run agent env` nor the runner process's own environment holds them. The runner answers 401 without the secret and 404 with it. CI's `agent-image` job repeats this.
- CodeRabbit: skipped. The free CLI allowance was used up ("Rate limit exceeded").
- Compose policy, a CI step on both Compose file sets. With sentinel `.env` and `agent.env` files, neither service publishes a port and neither mounts the Docker socket. The agent's resolved environment is exactly `agent.env`, with no bind mount, so no access to `.env` or the checkout. JotFlow's does hold the `.env` sentinel, which shows the check is not vacuous. It passed locally.
- `docker compose config --services` lists only `jotflow` by default, and both with `COMPOSE_PROFILES=agent`.

## Handoff notes

- #21: the JotFlow-side client sends `JOTFLOW_RUNNER_SECRET` only to `agent` and `JOTFLOW_EXEC_RUNNER_SECRET` only to `agent-exec`. It routes by kind, sending drafting and question runs to `http://agent:8766` and execution runs to `http://agent-exec:8766`. It posts `{run_id, kind, token}` with `Authorization: Bearer <JOTFLOW_RUNNER_SECRET>`. A 202 means started; then poll `GET /runs/{id}` for `exited`, `timed_out`, `killed`, or `failed` and the exit code. Call `DELETE /runs/{id}` on JotFlow's timeout or shutdown. The responses are 409 for an already-launched run, 429 at the live cap, and 503 when no GitHub token is set for the kind. A runner answers 403 for a kind it does not serve. Add `JOTFLOW_RUNNER_URL` and `JOTFLOW_RUNNER_EXEC_URL`, or one URL per kind, and `JOTFLOW_RUNNER_SECRET` to JotFlow's `Settings`.
- #22: set `AGENT_NPM_PACKAGES` with pinned versions, and replace `JOTFLOW_AGENT_COMMAND` with named presets that use `{mcp_config}`. Turn off session transcripts where the CLI allows it, since `HOME` is the service's persistent home volume.
- #126 (new, M3): `jotflow init` should generate two distinct secrets: one in `agent.env` and in `.env` as `JOTFLOW_RUNNER_SECRET`, and one in `agent-exec.env` and in `.env` as `JOTFLOW_EXEC_RUNNER_SECRET`, with mode 0600, and add `COMPOSE_PROFILES=agent` once the agent is set up.
- #55: the live step is `COMPOSE_PROFILES=agent docker compose up -d` with real tokens, a run started from JotFlow, and its `record_result`.
