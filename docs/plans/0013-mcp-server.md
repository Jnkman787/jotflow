# MCP server skeleton over Streamable HTTP

## Links and status

- Issue: #13
- Also serves: #16, which plugs its token check into the interface added here
- Draft PR: pending
- Status: planning
- Last verified: SDK behaviour checked with a throwaway prototype on 2026-10-18, nothing built yet
- Next safe action: commit this plan, push, open the draft PR

## Objective and done-when

Serve JotFlow's MCP endpoint from the `jotflow run` process, hardened to the 2026-07-28 specification and the review notes on #13 and #16, with no tools yet and no way in until #16 issues tokens.

Done when, for this PR:

- [ ] `jotflow run` starts the MCP server on `JOTFLOW_MCP_HOST:JOTFLOW_MCP_PORT` and stops it on shutdown, including when Slack setup fails
- [ ] Only protocol version `2026-07-28` is served. A request without `MCP-Protocol-Version`, or with any other version, gets 400 with an `UnsupportedProtocolVersionError` listing the supported versions. No `initialize` handshake and no `Mcp-Session-Id` are ever served.
- [ ] Header and body disagreement on `MCP-Protocol-Version`, `Mcp-Method`, or `Mcp-Name` gets 400 with `HeaderMismatch` (-32020)
- [ ] Any method other than POST gets 405 with `Allow: POST`
- [ ] Any request carrying an `Origin` header gets 403
- [ ] The `Host` header must be on the allowlist: loopback names on the configured port by default, plus configured names such as the Compose service name. Anything else gets 403.
- [ ] A query string gets 400, so a token can never travel in the URL
- [ ] Request bodies over 64 KiB get 413, and a request that takes longer than 30 seconds gets 504
- [ ] The server declares only the `tools` capability, with `listChanged: false`. No prompts, resources, subscriptions, logging, completions, sampling, or elicitation.
- [ ] Tools come from a fixed registry defined in code, empty for now. Nothing at runtime can add a tool or change a description.
- [ ] One audit log line per request: method, tool name if any, run id once #16 exists, status, outcome, and duration. Never arguments, bodies, or tokens.
- [ ] Every request needs `Authorization: Bearer <token>`. Until #16 lands, the token check rejects every token with 401, so nothing is reachable unauthenticated.
- [ ] Tests drive the endpoint in process, with no network beyond loopback

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

**Tool registry.** `whiteboard/tools.py` holds `TOOLS: tuple[ToolSpec, ...]`, empty for now. A `ToolSpec` has a name, a description, a Pydantic input model, and a handler. `build_server()` takes no tool argument, so nothing at runtime can add one. It validates the registry at start: unique names, names within the specification's character rules, non-empty descriptions. Input schemas come from `model_json_schema()`. Unknown tool names get a JSON-RPC error.

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
- [ ] Commit this plan and open the draft PR
- [ ] Token interface and tool registry
- [ ] Gate middleware and server builder
- [ ] Start and stop with `jotflow run`; settings
- [ ] Tests: each gate rule, protocol behaviour through the SDK, capabilities, audit lines, lifecycle
- [ ] Update documentation: architecture Whiteboard section and configuration, AGENTS layout
- [ ] `make check` passes, coverage at or above the floor
- [ ] CodeRabbit local review, or skipped
- [ ] `closingIssuesReferences` is empty, then mark the PR ready

## Validation

To be filled in.

## Handoff notes

None yet.
