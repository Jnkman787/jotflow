# Admin-issued, read-only operator tokens

## Links and status

- Issue: #58
- Draft PR: pending
- Status: planning
- Next safe action: the lead confirms the four defaults in Decisions, then implement

## Objective and done-when

D12: an admin gives a person a revocable token that can call only `list_topics` and `get_topic`, on named channels, so they can query the whiteboard from a terminal agent.

- [ ] `jotflow operator create --name alice --channel C0… [--channel …] [--days 30]` prints the token once. `jotflow operator list` shows names, channels, expiry and status, never tokens. `jotflow operator revoke alice` revokes it.
- [ ] Operator tokens can't call any other tool, including `get_work`. Each call is limited to the token's channels.
- [ ] Revocation takes effect within 2 seconds in the running server, and the CLI says so.
- [ ] Nothing new is reachable by default. Terminal access is an explicit opt-in.

## Decisions (defaults, for the lead to confirm)

1. **Storage and revocation.** Tokens are stored as SHA-256 digests in a new `operator_tokens` table: id, name, channels, created, expires, revoked. That's migration 9, or the next free number. The gate's `TokenCheck` must do no I/O, so the running server keeps an in-memory copy and reloads it from the database every 2 seconds. "Immediately" means within that interval. A real-time signal would need an admin channel into the running process, which is more attack surface than a 2-second window is worth.
2. **Grant shape.** A new kind, `RunKind.OPERATOR`. `Grant` gains `channels` for it; it's ignored for other kinds. `list_topics` and `get_topic` take an optional `channel` argument:
   - an operator must name one of its channels, or have exactly one;
   - a run grant may leave it out, or name its own channel only.

   Only those two tools declare `OPERATOR` in their kinds, so `tools/list` shows an operator nothing else, and the gate's kind check refuses the rest.
3. **Reachability.** The default stays unpublished. An opt-in overlay, `docker-compose.operator.yml`, publishes the MCP port on the host's `127.0.0.1` only, and `JOTFLOW_MCP_ALLOWED_HOSTS` gains `localhost:8765`. Remote use goes through an SSH tunnel. There's no TLS, so it's never published on a non-loopback interface, and the docs say so.
4. **Lifetime.** 30 days by default and at most 365, with a 1-second check against the store's clock. An expired token is refused, like a revoked one. The token is `jfo_` plus 32 random URL-safe bytes, printed once. Only its digest is stored.

## Risks

- The CLI writes to the same SQLite file as the running server. WAL and the busy timeout handle that, as `execute-fixture` already does.
- An operator sees topic titles and message pointers for their channels, but not message text. That's what `list_topics` and `get_topic` return today.

## Progress checklist

- [ ] Confirm the decisions
- [ ] Store table and contract
- [ ] Registry reload and the operator grant
- [ ] Tool kinds and the channel argument
- [ ] CLI create, list and revoke
- [ ] Compose overlay and docs
- [ ] Tests, including over the wire, for: refused tools, other channels, revoked within the reload interval, expired, and never logged
