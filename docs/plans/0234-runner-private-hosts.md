# Never send a runner secret or run token to a public address

## Links and status

- Issue: #234
- Draft PR: pending
- Status: planning
- Next safe action: the lead confirms the decisions below, then implement

## Objective and done-when

A Compose service name that isn't running doesn't resolve on Docker's network. The resolver then falls back to the host's DNS search domain, and a wildcard resolver can answer with a public IP. JotFlow would then try to send that host the runner secret, and with `POST /runs` the run's MCP token too. The same can happen the other way: the agent CLI sends its run token to `http://jotflow:8765/mcp`.

- [ ] JotFlow never connects to a runner at an address outside the private ranges below, unless the operator opts in. Tested with a fake resolver, and the connection goes to the address that was checked, never a second lookup.
- [ ] A missing runner fails its startup check within a few seconds. That run kind stays off with a clear reason.
- [ ] The runner refuses to start a run if JotFlow's MCP host resolves outside the private ranges, so the CLI is never handed a URL that points at a public host.
- [ ] The published Compose files set `dns_search: ["."]` on every service. CI shows service names and public names still resolve.
- [ ] docs/deployment.md and the threat model cover search-domain DNS.

**Out of scope:** TLS between JotFlow and runners, and the operator bridge, which always dials the literal `127.0.0.1`.

## Exposure, both directions

| Direction | Who connects | What's sent | Today |
|---|---|---|---|
| JotFlow to a runner: `GET /info`, `POST /runs`, `GET` and `DELETE /runs/<id>` | JotFlow's `RunnerClient` | The runner secret on every call. The run token in `POST /runs`. | Exposed. httpx2 resolves `agent` and `agent-exec` itself, with no check. |
| The agent to JotFlow's MCP server | The agent CLI, not our code | The run token, as a bearer token | Exposed in principle. If `jotflow` stops resolving through Docker's DNS, for example while JotFlow's container restarts, the CLI's lookup falls back the same way. Impact is lower: run tokens live only in JotFlow's memory, so a token sent while JotFlow is down is already dead. A token sent while JotFlow runs but its name fails to resolve would still be live, though that's unlikely. |

## Decisions (defaults, for the lead to confirm)

1. **Which addresses count as private.** An explicit allowlist, not `ipaddress.is_private`, which is inconsistent: it counts 198.18/15 but not 100.64/10.
   - IPv4: 10/8, 172.16/12, 192.168/16, 127/8 and 169.254/16.
   - IPv6: ::1, fc00::/7 and fe80::/10.
   - IPv4-mapped IPv6 is unwrapped first.
   - Every address a lookup returns must be in the allowlist. One public address refuses the whole lookup.

2. **No second lookup.** JotFlow resolves once, checks, and connects to that checked address, so the check and the connection can't disagree. The Host header keeps the original name, for example `agent-exec:8766`, which the runner's Host allowlist expects. This is a small httpx2 transport wrapper inside `RunnerClient`. Every runner call goes through it, not only `/info`.

3. **The opt-in for a remote runner** is `JOTFLOW_RUNNER_ALLOW_PUBLIC=1` in JotFlow's `.env`. It allows public addresses for both runners and logs a warning at startup naming each public runner host. Without it, the refusal reason says what to set. A remote runner still gets plain HTTP, so the docs say to put one on a private network or VPN instead, which needs no opt-in.

4. **Timeouts and retries.**
   - Every runner call gets a 3-second connect timeout. The 10-second read timeout stays.
   - A lookup that finds a public address is a refusal, so it isn't retried.
   - A name that doesn't resolve at all is retried like an unreachable runner, but the whole startup check gets a budget of about 15 seconds per runner, not 10 tries × (10 s + 3 s).
   - The reason names the case: "agent-exec does not resolve; is that service running?" or "agent-exec resolves to a public address".

5. **The agent-to-JotFlow direction, in the runner.**
   - At startup and before each launch, the runner resolves the host in `JOTFLOW_MCP_URL` and refuses the run if any address is outside the allowlist. A literal IP is checked the same way. The runner's own opt-in, `JOTFLOW_MCP_ALLOW_PUBLIC=1` in the agent env files, is off by default.
   - The CLI then does its own lookup, so a small window remains between the runner's check and the CLI's connection. `dns_search: ["."]` closes the search-domain route behind it. The threat model records what's left.

6. **`dns_search: ["."]`** goes on all three services in both published Compose files and their templates.
   - CI's cross-container job checks four lookups:
     - `jotflow`, `agent` and `agent-exec` resolve where they should;
     - a missing service name fails fast instead of resolving;
     - `slack.com` resolves from JotFlow's egress network;
     - `api.anthropic.com` and `github.com` resolve from the agent egress networks.
   - If any public lookup fails, `dns_search` stays out of the files and becomes a documented override, and the address check alone carries the fix.

## Design

- `jotflow/netcheck.py`, new: `private_only(addresses) -> bool` and `async resolve_private(host, port, *, allow_public) -> str`. The second returns the address to dial, or raises with a fixed reason. A `Resolver` protocol lets tests use a fake resolver.
- `orchestrator/launch.py`:
  - A `_PinnedTransport(inner, resolver, allow_public)` resolves `request.url.host` and points the request at the checked IP, keeping the original Host header. If the check fails, it raises `RunnerError` with a fixed reason.
  - `RunnerClient` takes `allow_public` from `Settings`, and its timeout gets a connect limit of 3 s.
- `orchestrator/service.py`, in `_runner_mismatch`: a refusal ends the retries, and a time budget replaces the fixed count.
- `adapters/runner.py`: `Runner._run` calls the same check on `mcp_url` before writing the plan, and `RunnerConfig` checks it at startup too. A refused run fails with `NOT_STARTED`, and the runner logs the reason.
- `config.py`: `runner_allow_public: bool = False`. In the runner's settings, `mcp_allow_public: bool = False`.
- The Compose files and templates get `dns_search`, and the CI job gets the lookup probes.
- Docs: deployment.md gets "DNS search domains", the threat model gets a "Someone on the host or the internal network" row and an "Accepted" note on the CLI's own lookup, and architecture.md gets a line on runner calls.

## Risks

- A deployment that already runs a runner on a public address breaks until it sets the opt-in. The startup reason names the variable. Before v0.1.0 there were no such installs.
- An odd Docker network on a range outside the allowlist, such as CGNAT 100.64/10 used by some VPNs, is refused until the operator opts in. The reason names the address range, not just "public". An operator who needs it can set the opt-in.
- `dns_search: ["."]` could change how a host's resolver treats bare names. That's why CI checks real lookups before it ships.

## Progress checklist

- [ ] Confirm the decisions
- [ ] `netcheck` with tests: every range, mapped IPv6, a mixed answer refused, a literal IP, and the opt-in
- [ ] `RunnerClient` dials the pinned address, with tests: a fake resolver returns a public address, so nothing is sent and the reason is fixed; the Host header is kept; the connect timeout applies
- [ ] The startup check: refusals aren't retried, and the time budget ends a missing runner fast
- [ ] The runner checks `mcp_url` at startup and per launch
- [ ] `dns_search` and the CI lookup probes
- [ ] Docs and threat model
- [ ] `make check`, then mark ready
