# The replay flake: a port race, removed

## Links and status

- Issue: #184
- PR: opened with this change
- Status: ready for review
- Last verified: `make check`, and 20 full-suite runs, 2026-10-20
- Next safe action: review

## Objective and done-when

`test_replay_all_scripted_and_with_the_stand_in` failed once in a full local run on 2026-10-19, and its output wasn't captured.

- [x] The cause is found, and the test fixed or its timing made robust. The likely cause, a port race, is removed.
- [x] Twenty consecutive full-suite runs pass.

## What I found

- **20 of 20 full-suite runs passed** on 2026-10-20, each between 2:22 and 3:29, at 598a9c5. Several overlapped other `make check` runs on the same machine. It didn't reproduce.
- **Timeouts:** the test gives each scripted stand-in run 60 seconds. One takes about 0.8 s on an idle machine, so the margin is about 75 times.
- **A shared resource:** `_free_port()` bound port 0, read the number, closed the probe, and uvicorn bound that port later. Another process could take the port in that gap. On 2026-10-19 several full suites were running at once on this machine, which fits a single failure that passed on rerun. Without the original output this can't be proven, but it's the only race found.

## Design

- **`whiteboard/server.py`** gains `bound_socket()`, a TCP socket bound to a free port and not listening. `McpServer` gains `sock`, which it passes to uvicorn's `serve(sockets=[...])`, so the port is held from the moment it's chosen. The server owns the socket and closes it on stop.
- **Replay** (`replay_agent`, `replay_execute`) and the tests that start an MCP server use it: test_codex_gate, test_launch, test_mcp_server, test_mcp_terminal_client, test_operator and test_runner.
- **The two "JotFlow is down" operator tests** hold a bound socket for their whole run. A bound socket that isn't listening refuses connections, and nothing else can bind its port.
- **test_service** still probes for a port, because the orchestrator binds the port from settings itself. Passing a socket through it would mean plumbing it through `Orchestrator`, which isn't worth it for a test. A comment there names the leftover race.

## Validation

- `make check` passes.
- New tests show a held socket keeps its port and refuses connections, and a server serves on the socket it's given and closes it on stop.

## Handoff notes

- A separate SQLite segfault under load, seen once, is tracked in its own issue.
