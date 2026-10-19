# Admin-issued, read-only operator tokens

## Links and status

- Issue: #58
- PR: #180
- Status: implemented, in review
- Next safe action: review #180

## Objective and done-when

D12: an admin gives a person a revocable token that can call only `list_topics` and `get_topic`, on named channels, so they can query the whiteboard from a terminal agent.

- [x] `jotflow operator create --name alice --channel C0… [--channel …] [--days 30]` prints the token once. `jotflow operator list` shows names, channels, expiry, status and last use, never tokens. `jotflow operator revoke alice` revokes it.
- [x] Operator tokens can't call any other tool, including `get_work`. Each call is limited to the token's channels.
- [x] A new token works at once. A revoked one stops within 2 seconds in the running server, and the CLI says so.
- [x] No port is published, and nothing new is reachable on the network. A terminal agent reaches the server through a stdio bridge in the container.
- [x] Each operator call is logged with the token id, tool, channel and time, and no topic content.

## Decisions (confirmed by the lead)

1. **Storage and revocation.** Tokens are stored as SHA-256 digests in a new `operator_tokens` table, migration 12. Its columns are id, name, digest, channels, created, expires, revoked and last used. The gate's `TokenCheck.check` does no I/O, so the running server keeps an in-memory copy and reloads it every 2 seconds. A revocation takes effect within that interval. A token the copy doesn't know triggers one reload, so a token created a moment ago works at once. Those miss reloads happen at most once a second, so a flood of bad tokens can't force reloads. Run tokens (`jfr_`) never trigger one.
2. **Grant shape and tools.** A new kind, `RunKind.OPERATOR`, with `Grant.channels`. `list_topics` takes an optional `channel` argument:
   - an operator must name one of its channels, unless it has exactly one;
   - a run may leave it out, or name its own channel only.

   `get_topic` reads a topic in any of the operator's channels, and answers a topic elsewhere exactly like a missing one. Only those two tools declare `OPERATOR`. `get_work` declares the three run kinds only. `tools/list` shows an operator only the two tools, and the server refuses any other call as an unknown tool. A test walks every registered tool against an operator token over the wire.
3. **Reachability: a stdio bridge, no port.** We never publish ports, not even on `127.0.0.1` or as an opt-in. A terminal MCP client instead runs this as its server command, on the Docker host or over ssh to it:

   ```sh
   docker compose exec -T -e JOTFLOW_OPERATOR_TOKEN jotflow jotflow operator connect
   ```

   `-e NAME` with no value copies the variable from the caller's environment, so the token is never in a command line. `jotflow operator connect` is a small stdio MCP server. It forwards `tools/list` and `tools/call` to the MCP endpoint on loopback inside the container, with the token as its bearer token, and it has no tools or capabilities of its own. It serves both the `initialize` handshake and the 2026-07-28 revision on stdio, so older clients work too. Everything else is the server's decision, behind the same gate as any run. There's no new network surface and no TLS question.
4. **Lifetime and audit.** 30 days by default, at most 365. An expired token is refused at the next check, without waiting for a reload. The token is `jfo_` plus 32 random URL-safe bytes, printed once. Names are never reused, so the audit log stays unambiguous. Each operator call logs `operator call token=<id> tool=<tool> channel=<channel> at=<time>` to `jotflow.operator.audit`, never a topic id, title or message. The gate's own audit line names the grant as `operator:<id>`. Last use is kept in memory and written back on each reload and at shutdown, and `operator list` shows it.

## Risks

- The CLI writes to the same SQLite file as the running server. WAL and the busy timeout handle that, as `execute-fixture` already does.
- An operator sees topic titles and message pointers for their channels, but not message text. That's what `list_topics` and `get_topic` return today.
- Anyone who can run `docker compose exec` on the host can already read the database and the Slack tokens. The bridge gives them nothing new. The operator token limits what the terminal agent behind it can read.

## Progress checklist

- [x] Confirm the decisions
- [x] Store table and contract
- [x] Token reload, the miss reload and the operator grant
- [x] Tool kinds and the channel argument
- [x] CLI create, list and revoke
- [x] The `connect` stdio bridge
- [x] The operator audit line and last use
- [x] Docs
- [x] Tests, over the wire and through the bridge, for: every tool against an operator token, other channels, revoked within the reload interval, a rate-limited miss reload, expired, and never logged
