# Wiring runs into `jotflow run`, and the first end-to-end execution

## Links and status

- Issue: #55
- Also serves: #54, whose last box is the cross-container launch
- Draft PR: pending
- Status: planning
- Last verified: plan only
- Next safe action: the lead confirms the plan and the open questions, then implement part 1

## Objective and done-when

Until now, `jotflow run` only logs Slack events. Nothing opens the store, serves the tools, or starts a run. This issue makes `jotflow run` the whole loop:

1. Ingest feeds the debouncer.
2. The debouncer queues drafting runs.
3. The supervisor starts queued runs on their runner.
4. The agent calls back over MCP with its run token.
5. Each run ends through `RunLifecycle`.

It then proves one execution run end to end: a hand-approved fixture proposal becomes a real issue.

The issue's boxes, refined:

- [ ] A fixture proposal produces a real issue in the sandbox repo. A new command adds a hand-approved proposal and queues its execution run. The maintainer runs it live with the #22 claude-code preset, against a sandbox repo and test channel of their choosing, by the runbook in Validation. This box closes only on that live run.
- [ ] The agent checks for the hidden proposal marker first, so a second run does not duplicate the issue. The preset's execution prompt owns the behaviour (#22, #30). Here, the live runbook runs the fixture twice and checks that one issue exists. A test shows a second run of an applied proposal is not started.
- [ ] The `record_result` URL is checked against the approved repo. `record_result` (#15) already does this. Here, an in-process end-to-end test drives it through the real MCP server with a stub agent, in two cases: a wrong-repo URL is refused, and the right one makes the proposal applied.

From the handoffs on #55:

- [ ] `jotflow run` opens the store and builds the read tools with `SlackWorkSource`, and the write tools with `SlackThreadPoster`. It serves them through `make_mcp_server`, with the same `TokenRegistry` the lifecycle issues from.
- [ ] Ingest's accepted messages go to the debouncer, and a loop flushes it. The supervisor runs beside them. At startup, `lifecycle.recover()` runs before anything else touches runs. This depends on product's answer in Risks.
- [ ] Shutdown order: stop taking Slack events first. Then stop the supervisor, which sends DELETE for live runs and revokes every live token. Then stop the MCP server, and close the store last. A test checks the order, and checks that no token is valid by the time the MCP server stops.
- [ ] The token reaches the agent only through the runner's 0600 per-run MCP config (#54). JotFlow sends it only in the launch request body, never in a URL, a header, argv, or a log line. A test scans captured logs for the token.
- [ ] Agent runs are off unless `[agent] preset` is set and that kind's runner secret is present. With them off, `jotflow run` behaves as today, and it says once at startup why runs are off.

From #54:

- [ ] JotFlow can launch a run in the agent container, and the agent reaches the MCP port. A CI step runs JotFlow's MCP server and supervisor in the `jotflow` container, with no Slack, and the real `agent` and `agent-exec` services. A stub agent command calls `tools/list` with its token. Both runs end `succeeded`, and the step checks that each kind saw only its own tools.

**Out of scope:**

- The presets and their prompts (#22). The CI check uses a stub command, and only the live run uses the preset.
- Retrying failed executions under a limit, and approval in Slack (#30, M4).
- Cards (#26).

## Design

**Three PRs**, in order, each reviewable alone:

1. **Wiring `jotflow run`.** It adds a new `jotflow.app` module, so `serve` stays thin. It holds an `App` with the store, registry, lifecycle, tools, MCP server, supervisor and debouncer, built from `Settings`, `Policy`, and the Bolt client. `async with App(...)` starts them in order and stops them in reverse. Tests use fakes for Slack and the runner.
2. **The cross-container check.** It adds a CI step and a small probe script. The script is piped into the `jotflow` image with `python -`, so it is not a product command. Its stub agent command lives only in CI's `agent.env`. This is #54's box.
3. **The fixture command and live runbook.** This is the `jotflow execute-fixture` command, plus the runbook in this plan.

**Startup**, inside `serve` after the Slack identity check:

1. Open `SqliteStore` at `database_path`.
2. Build a `TokenRegistry`, then a `RunLifecycle`, and run `recover()`.
3. Build the tools:
   - the read tools with `SlackWorkSource(BoltSlackReader(client), store, bot=identity)`
   - the write tools with `SlackThreadPoster(client)`
4. `make_mcp_server(settings, tokens, tools)`, then `start()`.
5. Build `RunnerClient.from_settings` and `RunSupervisor(..., max_running=policy.agent.max_concurrent_runs)`, and start its task. It is skipped when runs are off. With runs off, the debouncer still runs, and queued runs wait in the store.
6. Wire the debouncer into `register_handlers` and start its flush loop.
7. Connect Socket Mode.

**Shutdown, in reverse:**

1. Close Socket Mode.
2. Cancel the flush loop.
3. Stop the supervisor. It sends DELETE for each live run, then the registry revokes every token.
4. Stop the MCP server.
5. Close the store.

Runs stay `running` in the store, so the next startup's `recover()` marks them interrupted and queues fresh drafting runs. My own #116 note said to "end every running run as interrupted" at shutdown. That would skip the requeue, since `recover_runs` only requeues runs it finds active. Revoking every token before the MCP server stops gives the same guarantee and keeps the requeue. It needs a `TokenRegistry.revoke_all()`.

**`jotflow execute-fixture FILE|-`**, in part 3:

- It reads a small JSON file with channel, thread_ts, repo, title, body, labels and approved_by. It validates it against `jotflow.toml`, so the repo must be one the channel may feed.
- It adds the proposal and moves it pending, then approved, through the normal transitions with the content hash. Then it queues one execution run. The running `jotflow run` picks it up on its next tick.
- It refuses when `CI` is set or inside the image without an explicit flag, like `check --live`, because it causes a real GitHub write. It prints the proposal id and run id, never content.
- In Compose, run it as `docker compose exec -T jotflow jotflow execute-fixture - < fixture.json`.

## Risks and open questions

- **The debouncer wiring.** I asked product whether #55 or a product PR wires the debouncer into the handlers. My default is that #55 does.
- **How the runner picks the preset.** I asked the architect. My default is that the agent env file names the preset, while `policy.agent.preset` only turns runs on in JotFlow. If the architect's #22 does it another way, part 1 is unaffected and part 3 follows #22.
- **SQLite with two processes.** `execute-fixture` writes while `jotflow run` holds the database. The store already uses WAL with a busy timeout, which I'll confirm in part 3. Otherwise, the command can go through a startup flag instead.
- **The live run needs secrets I don't hold:** a model key, a GitHub write token, and a sandbox repo. So box 1 and the live half of box 2 close on the maintainer's run. Nothing deployment-specific goes in the repo.
- CodeRabbit runs once per PR if it's not rate-limited, and is skipped otherwise.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision except the two questions above
- [ ] Commit this plan and open the draft PR
- [ ] Part 1: `App`, startup and shutdown order, debouncer wiring, `revoke_all`, tests
- [ ] Part 1: in-process end-to-end test with a stub agent through the real MCP server, covering both `record_result` cases
- [ ] Part 2: cross-container CI check, for #54's box
- [ ] Part 3: `execute-fixture` and the live runbook
- [ ] Docs: README's run section, architecture's How runs start
- [ ] `make check` passes on each part
- [ ] Maintainer's live run

## Validation

Pending. The live runbook goes here in part 3.

## Handoff notes

Worktree `~/jotflow-work/wt-security-e2e`, branch `feat/e2e-execution`.
