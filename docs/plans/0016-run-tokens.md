# Per-run bearer tokens for the MCP server

## Links and status

- Issue: #16
- Also serves: #53, whose run lifecycle issues and revokes these tokens
- Draft PR: pending
- Status: planning
- Last verified: plan written on 2026-10-18 on main at 2b03ef2, nothing built yet
- Next safe action: commit this plan, push, open the draft PR

## Objective and done-when

Give each agent run its own short-lived bearer token, scoped to that run's kind, channel, and for execution runs one approved proposal, and enforce the token table in architecture.md on every MCP call. This fills the `TokenCheck` interface that #97 left refusing everything.

The issue's boxes:

- [ ] Calls without a token or with another run's token are rejected. A token resolves only to its own run's grant, and a call to a tool its run kind may not use is refused.
- [ ] Tokens expire when the run ends, through `revoke(run_id)`, or after a time limit
- [ ] The token never appears in logs, reprs, or error messages

Also, from the notes on #16:

- [ ] Tokens come from `secrets`, carry a recognisable prefix so secret scanners can spot a leaked one, and are kept only as SHA-256 digests in memory
- [ ] `check` compares in constant time, is synchronous, and does no I/O
- [ ] `Grant` gains `proposal_id` and `thread_ts`. An execution grant must name its proposal.
- [ ] `ToolSpec.kinds` declares which run kinds may call a tool. `tools/call` refuses other kinds before validating arguments, and `tools/list` shows a run only its kind's tools, which stay fixed, so there is still no `list_changed`.
- [ ] `jotflow run` creates one in-memory registry and hands it to the MCP server. It issues nothing yet, because nothing starts runs until M3, so the server stays unreachable in practice.

**Out of scope:** starting runs and passing tokens to agents (#21, #53, #54), the operator token (M5, D12), and the tools themselves (#14, #15). Channel and proposal scoping inside each tool is the tool's job, using the grant, and lands with #14 and #15.

## Design

See the token table in [architecture.md](../architecture.md), decision D13 in [roadmap.md](../roadmap.md), and the handoff notes in [0013-mcp-server.md](0013-mcp-server.md).

**`whiteboard/tokens.py`:**

- `Grant(run_id, kind, channel_id, thread_ts=None, proposal_id=None)`, frozen. An execution grant without a `proposal_id` is a `ValueError`. Other kinds may not carry one.
- `TokenRegistry`:
  - `issue(grant, ttl) -> str`: a new token, `jfr_` plus 32 random bytes from `secrets.token_urlsafe`. A run has at most one live token, so issuing again for the same run revokes the old one. `ttl` must be positive and at most a cap, and the registry holds at most a bounded number of live tokens.
  - `check(token) -> Grant | None`: hashes the presented token, looks the digest up, confirms it with `hmac.compare_digest`, and refuses it if expired. Expired entries are dropped when found.
  - `revoke(run_id) -> bool`: drops the run's token.
  - `active() -> int`, for tests and a future status line.
  - Only digests are stored, so a dump of the registry or a stray `repr` reveals no usable token. `repr` names the count only.
  - The clock is injectable for tests.
- `RejectAllTokens` stays as the explicit "no runs" check.

**Tool scoping, `whiteboard/tools.py` and `server.py`:**

- `ToolSpec.kinds: frozenset[RunKind]`, required and non-empty, checked by `checked_registry`.
- `list_tools` returns the tools for the grant's kind. Each kind's list is built once at startup.
- `call_tool` refuses a tool outside the grant's kind with a JSON-RPC error, and treats it exactly like an unknown tool, so a run cannot probe which tools exist for other kinds. It does this before validating arguments.

**`commands/run.py`:** `serve()` creates the `TokenRegistry` and passes it to `make_mcp_server`, so #53 can reach it. Nothing calls `issue` yet.

Alternatives considered:

- Signed self-contained tokens, such as HMAC over the grant: revocation would still need state, and restarts already kill every run, so a stored digest is simpler and revocable.
- Persisting tokens in SQLite: tokens should die with the process, since re-queued runs get fresh ones (D8), and `check` must not touch I/O.

## Risks and open questions

- Token length versus the gate's `MAX_TOKEN_LENGTH` of 512: `jfr_` plus 43 characters fits easily.
- The time limit is chosen by #53 per run kind. The registry only caps it, at two hours.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] `Grant` fields and `TokenRegistry`
- [ ] `ToolSpec.kinds`, kind-filtered list and call
- [ ] Registry created in `jotflow run` and passed to the server
- [ ] Tests: issue, check, expiry, revoke, reissue, caps, no token in logs or reprs, kind scoping through the real endpoint
- [ ] Update documentation: architecture token section, AGENTS layout if needed
- [ ] `make check` passes, coverage at or above the floor
- [ ] CodeRabbit local review, or skipped
- [ ] `closingIssuesReferences` checked, then mark the PR ready

## Validation

To be filled in.

## Handoff notes

None yet.
