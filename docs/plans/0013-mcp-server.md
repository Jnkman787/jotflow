# MCP server skeleton over Streamable HTTP

## Links and status

- Issue: #13
- Also serves: #16, which plugs its token check into the interface added here
- Draft PR: #97
- Status: completed
- Last verified: rebased on main at 80a93dc; `make check` green, 496 tests, 97.6% coverage; CI green on #97
- Next safe action: none; #13's last two boxes are covered by `tests/test_mcp_terminal_client.py`

## Objective and done-when

Serve JotFlow's MCP endpoint from the `jotflow run` process, hardened to the 2026-07-28 specification and the review notes on #13 and #16, with no tools yet and no way in until #16 issues tokens.

Done when, for this PR:

- [x] `jotflow run` starts the MCP server on `JOTFLOW_MCP_HOST:JOTFLOW_MCP_PORT` once Slack setup succeeds, and stops it on every exit path after that. A port that cannot be bound ends the run with a clear error and exit code 3.
- [x] Only protocol version `2026-07-28` is served. A request without `MCP-Protocol-Version`, or with any other version, gets 400 with an `UnsupportedProtocolVersionError` listing the supported versions. No `initialize` handshake and no `Mcp-Session-Id` are ever served.
- [x] Header and body disagreement on `MCP-Protocol-Version`, `Mcp-Method`, or `Mcp-Name` gets 400 with `HeaderMismatch` (-32020)
- [x] Any method other than POST gets 405 with `Allow: POST`
- [x] Any request carrying an `Origin` header gets 403
- [x] The `Host` header must be on the allowlist: loopback names on the configured port by default, plus configured names such as the Compose service name. Anything else gets 403.
- [x] A query string gets 400, so a token can never travel in the URL
- [x] Request bodies over 64 KiB get 413, and a request that takes longer than 30 seconds gets 504
- [x] The server declares only the `tools` capability, with `listChanged: false`. No prompts, resources, subscriptions, logging, completions, sampling, or elicitation.
- [x] Tools come from a fixed registry defined in code, empty for now. Nothing at runtime can add a tool or change a description.
- [x] One audit log line per request: method, tool name if any, run id once #16 exists, status, outcome, and duration. Never arguments, bodies, or tokens.
- [x] Every request needs `Authorization: Bearer <token>`. Until #16 lands, the token check rejects every token with 401, so nothing is reachable unauthenticated.
- [x] Tests drive the endpoint in process, with no network beyond loopback

The issue's own boxes:

- "Server starts with `jotflow run` and stops with it": satisfied by this PR.
- "A terminal MCP client can connect and list tools": satisfied in tests with a test token check. A real terminal client needs a real token, which comes with #16 and the operator token (D12).
- "Tool input schemas match the Pydantic models exactly": not yet, because there are no tools. The registry derives schemas from Pydantic models, and #14 and #15 add the tools and the tests.

**Out of scope:** the real token check and run scoping (#16), any tool (#14, #15), the agent container and Compose wiring for the `agents` network (#54).

## Design

See Whiteboard MCP server, the token table, and Deployment in [architecture.md](../architecture.md), and decision D13 in [roadmap.md](../roadmap.md).

**SDK.** The project depends on `mcp` 2.x, where FastMCP was renamed `MCPServer`. `MCPServer` always registers prompt, resource, and subscription handlers, and the SDK advertises a capability for every registered handler, with `listChanged: true` when `subscriptions/listen` is served. JotFlow therefore builds on the SDK's low-level `Server` and registers only `tools/list` and `tools/call`. The SDK's modern request path still does the protocol work: envelope validation, header and body mismatch rejection, `Mcp-Param-*` checks, and JSON-RPC framing. `json_response=True` and `stateless_http=True` mean single JSON replies and no sessions.

**The gate.** A small ASGI middleware in front of the SDK app, `whiteboard/gate.py`, applies JotFlow's rules before any SDK code sees the request, in this order:

1. Not HTTP, such as the lifespan: passed through.
2. `Origin` present: 403.
3. `Host` not on the allowlist: 403.
4. Method other than POST: 405 with `Allow: POST`.
5. Query string present: 400.
6. `MCP-Protocol-Version` missing or not pinned: 400 with the JSON-RPC `UnsupportedProtocolVersionError` the specification defines. This closes the SDK's legacy path, which would otherwise answer an `initialize` handshake or open a GET stream.
7. `Content-Length` over the limit: 413. The SDK's own body limit is set to the same value for chunked bodies.
8. `Authorization` missing, not `Bearer`, or rejected by the token check: 401 with `WWW-Authenticate: Bearer`.
9. Otherwise the SDK handles it, within the request timeout. A timeout before the response starts gets 504.

The gate writes the audit line for every request, including rejected ones. Header values that reach the log are reduced to a safe character set and length, because they are client-supplied.

**Token interface.** `whiteboard/tokens.py` defines `TokenCheck`, a protocol with one method that takes the presented token and returns a `Grant` (run id, run kind, channel) or `None`. `RejectAllTokens` is the default and returns `None` for everything. #16 replaces it. The gate stores the grant in the ASGI scope, so tools can scope themselves by it.

**Tool registry.** `whiteboard/tools.py` holds `TOOLS: tuple[ToolSpec, ...]`, empty for now. A `ToolSpec` has a name, a description, a Pydantic input model, and a handler. `build_app()` uses `TOOLS` by default. Tests pass their own `ToolSpec` list, which is still code; nothing reads tools from configuration, Slack, or the database, and the list is fixed once the server is built. It validates the registry at start: unique names, names within the specification's character rules, non-empty descriptions. Input schemas come from `model_json_schema()`. Unknown tool names get a JSON-RPC error.

**Settings.** `JOTFLOW_MCP_HOST` and `JOTFLOW_MCP_PORT` already exist. A new `JOTFLOW_MCP_ALLOWED_HOSTS` takes comma-separated extra `Host` names, such as `jotflow` for the Compose service. A name without a port gets the configured port. Loopback names are always allowed.

**Running it.** `whiteboard/server.py` builds the gate and app, and runs uvicorn in the same event loop as the Socket Mode handler. uvicorn's own signal handling is turned off, so `jotflow run`'s existing stop handling stays in charge. Connection limits are set as well: concurrency, keep-alive, and incomplete-event size. `commands/run.py` starts the server after Slack setup succeeds and stops it in the same `finally` that stops Socket Mode.

**Dependencies.** `uvicorn` and `starlette` are already locked as dependencies of `mcp`. JotFlow now imports them directly, so they are declared in `pyproject.toml`; the lock changes only in the specifiers.

Alternatives considered:

- `MCPServer`: rejected for the capability problem above.
- Allowing the `2025-11-25` handshake for older agent CLIs: deferred. It brings sessions back, and #16's notes require binding them to the token. M3 checks which protocol versions the supported CLIs speak and decides then.
- The SDK's `TransportSecuritySettings` alone: it allows any listed `Origin`, while JotFlow rejects every `Origin`, so the gate does both checks itself. The SDK settings are still set as a second layer.

## Risks and open questions

- If an agent CLI in M3 speaks only the handshake era, it cannot connect. The pinned versions are one constant, and the decision is recorded above.
- uvicorn's private signal handling could change between releases. The override is one method, and a test checks that `jotflow run`'s handlers stay installed.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Token interface and tool registry
- [x] Gate middleware and server builder
- [x] Start and stop with `jotflow run`; settings
- [x] Tests: each gate rule, protocol behaviour through the SDK, capabilities, audit lines, lifecycle
- [x] Update documentation: architecture Whiteboard section and configuration, AGENTS layout
- [x] `make check` passes, coverage at or above the floor
- [x] CodeRabbit local review, or skipped: skipped, rate limit
- [x] `closingIssuesReferences` is empty, then mark the PR ready

## Validation

- **#13's last two boxes, in a follow-up after #106 (read tools) and #109 (write tools) merged.** `tests/test_mcp_terminal_client.py` runs the real server on loopback with the full tool set, built as `jotflow run` will build it in #55, and tokens issued by `TokenRegistry`.
  - The MCP SDK's own streamable-HTTP client connects with each run kind's token and lists exactly that kind's tools, as written out from architecture.md's token table. A revoked token is refused with "the bearer token is not valid".
  - Every listed tool's `inputSchema` equals its Pydantic model's `model_json_schema()`. Its `outputSchema` equals the serialization schema, or is absent when the tool has no output model.

- `make check` passes: ruff, mypy strict, and 467 tests with 98% total (497 and 97.6% after rebasing on main and the review changes) line and branch coverage. `whiteboard/gate.py`, `tokens.py`, and `tools.py` are at 100%, and `server.py` at 97%. The two lines left are the no-grant guard, reachable only without the gate, and a start that ends without an error.
- `tests/test_mcp_server.py`, 48 tests, all in process through `httpx2.ASGITransport` except the two lifecycle tests on loopback:
  - `server/discover` returns `supportedVersions: ["2026-07-28"]` and capabilities exactly `{"tools": {"listChanged": false}}`, with no `Mcp-Session-Id`
  - the SDK's own `Client`, pinned to 2026-07-28, connects and lists no tools
  - a test-only tool shows its Pydantic schema, gets the grant and validated arguments, and an invalid argument error names the field and never the value
  - header and body disagreement gets 400 with -32020 from the SDK
  - the default token check refuses everything with 401. Missing, non-Bearer, empty, spaced, and oversized tokens get 401 and never reach the check.
  - any `Origin`, including the server's own, gets 403. Off-list `Host` values get 403, and a configured `jotflow` name is allowed.
  - GET, DELETE, PUT, and OPTIONS get 405 with `Allow: POST`; before the gate, a legacy GET hung open as an SSE stream
  - a query string gets 400
  - a missing version, `2025-11-25`, `2025-03-26`, and an unknown version get 400 with -32022 and the supported list, and no session id. A legacy `initialize` never reaches the SDK.
  - declared and streamed bodies over 64 KiB get 413, and a non-numeric `Content-Length` gets 400
  - a slow request gets 504, and a request that times out mid-response gets no second response start
  - each request writes one audit line. Lines for a served call, a bad token, and an `Origin` rejection hold no argument text, token, or wrong-token text, and an unsafe `Mcp-Name` is logged as `?`.
  - malformed registries fail at startup
  - a real uvicorn start on loopback answers a request, leaves the process's SIGTERM handler untouched, stops, and frees the port. A taken port gives `McpStartError` rather than exiting the process.
- `tests/test_run.py`: `serve()` starts the MCP server after Slack setup and stops it after Socket Mode closes, also when Socket Mode is refused. A Slack setup error never starts it, and an MCP start error exits 3 before Socket Mode connects.
- CodeRabbit: skipped. The free CLI allowance was used up ("Rate limit exceeded").
- `uv lock` adds no package. `uvicorn` and `starlette` move from transitive to declared, `httpx2` joins the dev group, and `mcp` is pinned to `>=2.3,<3` because the code uses the 2.x API.

## Review changes

Architect review on 2026-10-18 found nothing blocking. Taken in this PR:

- The Stack table in architecture.md said "FastMCP server". It now names the low-level `Server` and the reason, so nobody switches it back to `MCPServer`.
- `McpServer.start()` has a 10 second deadline. A lifespan startup that hangs is cancelled and raises `McpStartError`, so `jotflow run` cannot hang at startup.
- README Troubleshooting has a row for exit code 3, the port in use.

## Handoff notes

- #16 implements `TokenCheck.check()` and passes it to `build_app(tokens=...)` in `commands/run.make_mcp_server`. Tool handlers receive the `Grant`, and run-kind scoping belongs there or in the call handler.
- #14 and #15 add `ToolSpec` entries to `whiteboard/tools.TOOLS`, with Pydantic input models from `models.py`. The registry check and schema derivation are already tested.
- Shape agreed with architect for #16, so it does not need rework:
  - `Grant` gains `proposal_id: str | None`, for execution tokens scoped to one approved proposal, and `thread_ts: str | None` for drafting.
  - Run-kind authorization lives in the registry: `ToolSpec.kinds: frozenset[RunKind]`. `call_tool` checks it before validating arguments, and `list_tools` lists only the grant kind's tools. The list is static per kind, so there is still no `list_changed`.
  - The issuing side is a `TokenRegistry` with `issue(grant, ttl) -> str`, `revoke(run_id)`, and `check(token)`. It is in memory, so tokens die on restart and re-queued runs get fresh ones. `check` stays synchronous with no I/O.
  - Optional: `ToolSpec.output_model`, so each tool advertises an `outputSchema` matching its `structuredContent`.
- For #15: the 30 second timeout cancels a handler mid-call. A write tool cancelled after the store commits but before the response lets the agent retry and duplicate the write. Commit inside a shielded scope, or give write calls an idempotency key such as run id plus a per-run call counter.
- Shutdown order for M3: stop Slack intake, then end or abort runs and revoke their tokens, then stop the MCP server. Today's order, Slack then MCP, already points that way.
- M3 (#54) sets `JOTFLOW_MCP_HOST` to listen on the `agents` network and `JOTFLOW_MCP_ALLOWED_HOSTS=jotflow`. It also checks that the shipped agent CLIs speak 2026-07-28. If one only speaks the handshake era, adding `2025-11-25` means sessions, which must be bound to the token, per the notes on #16.
