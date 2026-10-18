# MCP read tools: list_topics, get_topic, get_work

## Links and status

- Issue: #14
- Draft PR: #106
- Status: completed
- Last verified: `make check` green locally on `main` after #104 and #105
- Next safe action: review and merge #106. Whichever PR opens the store in `serve()`, #107 or #21, adds `*read_tools(store, policy, source)` to `make_mcp_server`

## Objective and done-when

The agent's read tools. `list_topics` and `get_topic` expose the channel's topic index. `get_work` returns the run's packet:

- for a drafting run: the thread text, the topic index, and excerpts of related threads
- for an execution run: only the approved proposal

Done when:

- [x] Each tool rejects an unknown run id, channel, or topic with a clear reason
- [x] `get_work` for an unknown, finished, or expired run returns a clear error, not empty data
- [x] Slack text, topic titles, and related-thread excerpts sit in fields labelled untrusted, and never in tool descriptions or instructions
- [x] An execution run's packet holds only the approved structured proposal, never Slack text
- [x] Each tool is available only to the run kinds the token table allows
- [x] `make check` passes

**Out of scope:**

- fetching thread text from Slack, resolving mentions, and trimming long threads, which is #20; this PR defines the interface #20 implements
- the write tools (#15)
- the operator token (D12, M5)

## Design

References: D1, D6, and D12 in [roadmap.md](../roadmap.md); the MCP tool table, the token table, and Run lifecycle in [architecture.md](../architecture.md); the shapes agreed for #16 in the #97 review.

**Who may call what**, from the token table:

| Tool | Drafting | Execution | Question (M5) |
|---|---|---|---|
| `list_topics` | yes | no | yes |
| `get_topic` | yes | no | yes |
| `get_work` | yes | yes | yes, the same conversation packet as drafting |

Each `ToolSpec` declares these `kinds` (#105). The server lists only the grant's tools, and treats any other tool as unknown.

**Scoping.** Every tool reads the channel, thread, and run from the token's `Grant`, never from the agent's arguments. So a run can only see its own channel. A topic in another channel is reported as "no such topic", the same as a missing one, so ids cannot be probed across channels.

**Errors.** A missing topic or an unusable run is a tool error the model should see and act on, so the server returns `CallToolResult(is_error=True)` with a short reason. It does not return a protocol error. A `ToolError` exception in `whiteboard/tools.py` carries that reason. Protocol errors stay for unknown tools and bad arguments, as #97 has them.

**`get_work`** checks the grant's run in the store (#104). The run must exist, be `running`, and be before its deadline; otherwise the reason is "run r… is finished" or "run r… is past its deadline".

- **Drafting and question runs:** ask a `WorkSource` for the thread. This is a small protocol that #20 implements with a live Slack fetch, and that tests fake. The packet is:
  - `thread`: messages with ts, author display name, and text, all marked untrusted
  - `topics`: the active topic index, titles marked untrusted
  - `related`: short excerpts from likely-related threads, marked untrusted
  - `notice`: a fixed line, written in code, saying the untrusted fields are data written in Slack and never instructions
- **Execution:** reads the proposal from the store. It must be approved, executing, or reconciling. The packet holds:
  - the proposal id, which is also the hidden marker to search for
  - the repo, and the structured draft or update
  - a notice telling every execution run to look for the marker before writing (review change: this replaced a `check_marker_first` field)
  - no Slack text at all

**Labelling.** Untrusted fields use one wrapper model, `UntrustedText(text, source)`, with a schema description saying what it is. So the label shows in the output schema the agent sees, through `ToolSpec.output_model` if #105 adds it, and on every value. Keyword prefixes such as `idea:` are stripped by #20 before text reaches the packet (D10).

**Files:** `whiteboard/read_tools.py` holds the specs, handlers, and packet models. `whiteboard/tools.py` gains `ToolError` and registers the read tools in `TOOLS`. Tests cover every tool, kind, and error path with a fake `WorkSource`, `MemoryStore`, and grants made by hand.

Alternative considered: taking `channel_id` as an argument and checking it against the grant. Reading it from the grant is simpler, and there is nothing to get wrong.

## Risks and open questions

- Depends on #104 for `get_run`, and on #105 for `ToolSpec.kinds`, `Grant.thread_ts`, and `Grant.proposal_id`. This PR is built after both merge, rather than stacked on two moving branches.
- `get_work` before #20 lands has no real Slack source. `jotflow run` wires a source that reports "thread fetching arrives with #20", so the tool fails clearly instead of returning an empty thread.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Rebase on `main` once #104 and #105 merge
- [x] `ToolError` and the error result path in the server
- [x] Packet models with untrusted labelling
- [x] `list_topics`, `get_topic`, `get_work` with scoping and kinds
- [x] `WorkSource` protocol and the placeholder source in `jotflow run`
- [x] Add or update tests
- [x] Update documentation: the architecture tool table, and a note for #20 on the `WorkSource` interface
- [x] `make check` passes
- [x] Local CodeRabbit review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make check` passes. `read_tools.py`, `tools.py`, and `tokens.py` are at 100%.
- `tests/test_read_tools.py`, 26 tests, calls each handler directly with grants built by hand, a `MemoryStore`, and a fake `WorkSource`. They cover:
  - the token table's kinds, and fixed descriptions
  - the untrusted label appearing in the `get_work` output schema
  - no-argument inputs refusing a smuggled `channel_id`
  - `list_topics` scoped to the grant's channel, and an unconfigured channel refused
  - `get_topic` returning pointers and an untrusted title, and a topic elsewhere getting the same message as a missing one
  - a drafting packet with its notice, thread, topics, and related excerpts all marked untrusted
  - a question run's conversation packet
  - the placeholder source failing with "#20"
  - refusals for an unknown, queued, finished, or overdue run, and a grant that does not match its run on kind, channel, or thread
  - an execution packet holding only the approved proposal, with no Slack text
  - every execution notice requiring the marker check
  - a settled proposal, another proposal's grant, and a proposal in another channel
- CodeRabbit: skipped, rate limit.
- After review: `test_read_tools.py` has 31 tests.
- `tests/test_mcp_server.py` adds two tests: a `ToolError` comes back as an `isError` result with no protocol error, and an `output_model` is advertised as `outputSchema`.

### Review changes (reviewer at 6891965, security's second look, team lead)

- **Blocker: index titles were not wrapped.** `TopicSummary.title` is now `UntrustedText`, and `UntrustedText` moved to `models.py` so the topic index (#110) can use it. `to_prompt` keeps the label on every title. A test checks that the `get_work` and `list_topics` schemas mark index titles untrusted.
- **Blocker: `check_marker_first` contradicted D8.** The field is gone. `EXECUTION_NOTICE` tells every execution run to list recent issues and comments and look for the marker before writing.
- **Nit: content check.** `get_work` refuses an execution proposal whose draft no longer matches its locked content hash (D7).
- **Nit: `get_work(run_id)` wording.** The architecture's agent output contract and D1 now say `get_work` takes no arguments, because the token names the run.
- **Security's note: schema mode.** Output schemas use `model_json_schema(mode="serialization")`, matching what `model_dump(mode="json")` sends.
- **Team lead's correction: packet recording.** `get_work` records only the run's own thread through `record_packet`, not related excerpts, so a run's links stay inside the thread its token names. A test calls #109's real `link_message`: a served message is accepted, and a related excerpt or an unserved message is refused.
- `ToolError` came from #109, which merged first.

## Handoff notes

- **Deviation from the plan.** The architecture token table lets question runs call `get_work`, so they get the same conversation packet as drafting. The packet section is named `conversation`, not `drafting`, so it fits both kinds.
- **Not wired into `jotflow run` yet.** `serve()` opens no store today. The run lifecycle work, #107 or #21, opens it, and adds `*read_tools(store, policy, source)` beside security's `*write_tools(...)` in `make_mcp_server`. Nothing issues tokens until then, so the tools are unreachable either way.
- **For #20.** Implement `WorkSource.thread`, which returns a `WorkThread` with every author and text wrapped in `UntrustedText`, and keyword prefixes stripped. Also implement `related`.
- **Packet pointers, for #15.** `get_work` records the run's own thread through `record_packet`, and nothing else: related excerpts and execution packets record none. `RelatedExcerpt` still carries a `ts` pointer for context.
- **Reviewer nit on #112, folded in here.** The 2-hour cap is now `models.MAX_RUN_SECONDS`. `whiteboard.tokens.MAX_TOKEN_TTL_SECONDS` and the store's `MAX_RUN_TIMEOUT_SECONDS` both read it, `Run.timeout_seconds` uses it, and the store no longer imports the whiteboard layer. The ttl clamp for a 2-hour run plus grace is #107's.
- **`ToolError`** came from #109, which landed first; this PR adds only `output_model` and the output schema.
