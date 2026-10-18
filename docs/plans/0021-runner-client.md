# JotFlow's runner client

## Links and status

- Issue: #21
- Also serves: #54, whose open box "JotFlow can launch a run in the agent container" needs this side
- Draft PR: #131
- Status: ready for review
- Last verified: `make check` green, 1438 tests, 98.5% coverage, on 2026-10-18
- Next safe action: reviewer reviews #131. The first issue that enqueues live runs wires `RunSupervisor` into `jotflow run`

## Objective and done-when

#54 put a runner in each agent container. This is JotFlow's side: it asks the right runner to start a run, follows the run to its end, finishes it through `RunLifecycle`, and posts a short notice in the thread when a run fails.

The issue's boxes, refined by its dated notes and the #54 split:

- [x] Template substitution for prompt, MCP config path, and token. The runner does this (#54): the token goes only into the 0600 MCP config, and the argv template takes `{mcp_config}` and `{run_id}`. This PR adds the client that hands the runner the run id, kind, and token, and a test that runs the real runner app and the real MCP server with a stub agent. The prompt text belongs to #22's presets.
- [x] Timeout kills the process and marks the run failed. JotFlow times the run out through `RunLifecycle.expire_overdue`, which revokes the token first, and then sends the runner `DELETE /runs/{id}`, which kills the process group. The run ends as `timed_out`, one of the failed statuses.
- [x] Failures surface in the Slack thread as a short notice. The text is fixed per outcome and names the run id only. It never carries agent output, exit codes, or runner replies.
- Already done in the runner (#54): the 0600 config in a 0700 directory removed at the end, and the environment allowlist.

More done-when, from the lead's brief and the #54 handoff:

- [x] Routing by kind, with no fallback. Drafting and question runs go to `JOTFLOW_RUNNER_URL` (default `http://agent:8766`) with `JOTFLOW_RUNNER_SECRET`. Execution runs go to `JOTFLOW_EXEC_RUNNER_URL` (default `http://agent-exec:8766`) with `JOTFLOW_EXEC_RUNNER_SECRET`. A secret is only ever sent to its own runner. A test asserts each request's URL and `Authorization` header by kind.
- [x] Missing secret for a kind means that kind's runs fail with a fixed reason. JotFlow never sends one runner's secret to the other.
- [x] `Settings` refuses identical runner secrets, since the exec runner's isolation rests on them differing.
- [x] Every run reaches a final status on every path: success, non-zero exit, a launch refusal, a lost run, a timeout, and runner unreachable at launch.

**Out of scope:**

- Wiring the supervisor into `jotflow run`. Nothing in the live path enqueues runs yet, and `serve` has no store. The first issue that enqueues live runs wires it in, likely #55 or #22's presets.
- Presets and prompts (#22, #31). Writing the secrets from `jotflow init` (#126). The cross-container live step (#55).

## Design

**Settings** in `config.py`, all optional so `jotflow run` works without agents:

- `runner_url` and `exec_runner_url`, http or https only, with no user info, query, or fragment. Defaults are the Compose names.
- `runner_secret` and `exec_runner_secret` as `SecretStr | None`. They are never logged or put in `repr`. A validator refuses them when they are equal.
- The env names follow #128's rename: `JOTFLOW_EXEC_RUNNER_SECRET`, not `JOTFLOW_RUNNER_EXEC_SECRET`.

**`RunnerClient`** in `src/jotflow/orchestrator/launch.py`, on `httpx2`. It moves from a dev dependency to a runtime one. It is already locked through the MCP SDK.

- It holds two optional endpoints, each a URL and a secret, chosen by `endpoint_for(kind)`: execution maps to the exec endpoint, the rest to the conversation endpoint. Neither falls back to the other.
- `launch(run_id, kind, token)` posts exactly `{run_id, kind, token}`. 202 means started. Other replies become `RunnerError` with a fixed reason chosen by status, such as 409, 429, 403, 503 or 401. The runner's body is never read into a reason or a log.
- `status(run_id, kind)` does `GET /runs/{id}`. The reply is validated strictly: the same run id, a known state, and an integer or null exit code. 404 means the runner lost the run.
- `stop(run_id, kind)` does `DELETE /runs/{id}`. 404 counts as already gone.
- Each call has a short timeout, 10 s. `follow_redirects=False` keeps a redirect from carrying the secret to another host. `trust_env=False` keeps a proxy from the environment from seeing the secret or the token.
- The token is a parameter only. It is never stored on the client, logged, or put in an exception.

**`RunSupervisor`** in the same module drives runs through `RunLifecycle`. Each step is one `tick()`, so tests step it, and `run(stop)` loops ticks every 2 s.

1. **Timeouts first.** `lifecycle.expire_overdue()` revokes each overdue run's token and marks it `timed_out`. Then the supervisor sends `DELETE` to each one it tracks and posts the timeout notice.
2. **Poll.** It calls `status` on every tracked run. The outcomes:
   - `exited` with code 0 finishes `succeeded`. Any other code finishes `failed`.
   - `timed_out` finishes `timed_out`, for when the runner's own limit fired first.
   - `killed` and `failed` finish `failed`.
   - A 404 finishes `failed`, because the runner lost the run.
   - A network error leaves the run alone. Its deadline still bounds it.
3. **Launch.** It calls `start_next(max_running=policy cap)` until it gets None. Each run is launched on its kind's runner. Any launch error finishes the run `failed` with the fixed reason, revoking the token, and posts the failure notice.

Notices go out only when `finish` returns a run, so a run is never noticed twice. Store errors use only fixed strings, such as "agent exited with a non-zero code", "the runner refused the run (429)", or "the runner lost the run".

**Shutdown.** `close()` sends `DELETE` to every tracked run. It leaves them running in the store, so startup recovery settles them as it does after a crash, and the in-memory tokens die with the process.

**Failure notice.** It uses `SlackThreadPoster` in the run's own channel and thread, with links, media, and names never expanded. There are two fixed texts:

- On failure: "JotFlow's agent run for this thread did not finish (run `<id>`). An admin can find the reason in JotFlow's logs."
- On timeout: "JotFlow's agent run for this thread ran out of time and was stopped (run `<id>`)."

A failed post is logged with Slack's error code and never raised.

**Tests**, in `tests/test_launch.py`:

- The client against the real `build_runner_app` through `httpx2.ASGITransport`, with stub agents from the runner tests. This covers 202, then running, then exited, plus 409, 403, the 401 for a wrong secret, and DELETE.
- Routing and headers through `httpx2.MockTransport`. A drafting request never goes to the exec URL or carries the exec secret, and the reverse holds too. A missing secret refuses with no request sent.
- The supervisor with the memory store, a fake clock and a recording poster. It covers success, a non-zero exit, a refusal, a lost run, an overdue run getting a revoked token, DELETE and the notice, network errors leaving runs alone, and one notice per run.
- Notices: a test asserts the posted text equals the fixed template, even when runner replies contain other text.
- Settings: equal secrets are refused, and bad URLs too. The secrets never appear in `repr` or a validation error.

## Risks and open questions

- **429 after a JotFlow-side timeout.** If a DELETE fails, the runner may still hold a live slot JotFlow counts as free. The next launch then gets 429 and fails with a notice. That is visible and bounded by the runner's own limit. Requeueing instead needs a store method, so it is left out.
- **Polling, not callbacks.** A callback would need an endpoint on JotFlow that the agent network can reach. Polling every 2 s keeps the runner's API one-way.
- **Secret name.** I assume `JOTFLOW_EXEC_RUNNER_SECRET`, as #128 renames the docs. If the lead prefers the other name, it is one field alias.
- CodeRabbit runs once if it is not rate-limited, and is skipped otherwise.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Settings fields and validators
- [x] `RunnerClient` with routing, strict replies, and fixed errors
- [x] `RunSupervisor` with timeouts, polling, launch, notices, and close
- [x] Tests, including the real runner app in process
- [x] Docs: architecture's How runs start and its environment-tier list. README and the #54 handoff are left alone, because #128 renames the secret there
- [x] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- `make check`: green, with 1438 tests and 98.5% coverage. `launch.py` is at 99%, and the uncovered branches are loop exits.
- `tests/test_launch.py` has 61 tests:
  - Routing and headers for each kind, against a mock transport.
  - Both real runner apps in process, chosen by host. Each accepts only its own kind and secret, so a swapped secret gets 401. A DELETE kills a sleeping stub agent.
  - The supervisor on every ending path, with one notice per run, through a scripted runner.
  - End to end in process: the supervisor starts a queued run on the real runner. Its stub agent calls the real MCP server with its token and gets 200, and the run ends `succeeded` with its token revoked.

## Handoff notes

Worktree `~/jotflow-work/wt-security-client`, branch `feat/runner-client`.

- To wire it in, build `RunnerClient.from_settings(settings)` and a `RunSupervisor` with the lifecycle, store, a `SlackThreadPoster`, and the policy's `max_concurrent_runs`. Run `supervisor.run(stop)` as a task beside the Socket Mode connection. Call `lifecycle.recover()` first.
- `httpx2` is now a runtime dependency. The lock only changed its own dependency list.
