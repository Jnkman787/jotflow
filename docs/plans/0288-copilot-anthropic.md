# Gate the copilot preset on Anthropic's API too

## Links and status

- Issue: #288 (M6), a follow-up to #59 and #285
- Draft PR: #297
- Status: ready for review. The lead approved the plan on 2026-10-21, and security approved it the same day with the condition below
- Last verified: 2026-10-21, Copilot CLI 1.0.101 in a throwaway agent image, with `--network none`, against JotFlow's real MCP server and a scripted fake of Anthropic's Messages API
- Next safe action: review

## Objective and done-when

The copilot preset accepts only OpenAI-compatible providers (`COPILOT_PROVIDER_TYPE` set to `openai` or `azure`), because its CI gate fakes only the OpenAI Responses API. This adds Anthropic's Messages API to the gate, so the preset can accept `anthropic` too.

Done when, from #288's boxes:

- [ ] `jotflow.testing` has a scripted fake Anthropic Messages API, like `fake_responses_api`
- [ ] The copilot gate runs each run kind with `COPILOT_PROVIDER_TYPE=anthropic` against it, with the same checks
- [ ] The preset accepts `anthropic`, and presets.md says so
- [ ] `make check` passes

**Out of scope:** a real-model pass (#293), Copilot's subscription login (#287), and any other provider type.

## What was checked

On 2026-10-21, Copilot CLI 1.0.101 ran this preset's drafting command with `COPILOT_PROVIDER_TYPE=anthropic` and no `COPILOT_PROVIDER_WIRE_API`. The model was a lab fake of the Messages API on 127.0.0.1, which answered with one `tool_use` calling `bash`, then text.

- **The wire:** Copilot sent `POST /v1/messages` with `stream: true`, the `x-api-key` header, `anthropic-version: 2023-06-01`, and an `anthropic-beta` header. It made no other request.
- **The offered tools** were exactly the drafting tools, the same set as on the Responses wire.
- **The `bash` call** came back as a `tool_result` saying "Tool 'bash' does not exist". No file was written, and Copilot exited 0 by itself.
- **The hidden built-ins differ by wire.** Copilot's "Disabled tools" notice listed `create`, `edit` and `grep` on this wire, where the Responses wire had `apply_patch` and `rg`. So the tool set depends on the provider, which is why each wire needs its own gate pass. `--available-tools` filtered both to the same exact set.
- Neither token appeared in the model requests.

## Design

- **`jotflow.testing.fake_messages_api`**, a `FakeMessagesApi` like `FakeResponsesApi`. It serves `POST /v1/messages` on 127.0.0.1, records each request, and streams the real event sequence: `message_start`, `content_block_start`, `content_block_delta` (`input_json_delta` or `text_delta`), `content_block_stop`, `message_delta` and `message_stop`. The first answer is a scripted `tool_use`, and any later one is text. `offered_tools()` reads `tools[].name`, and `tool_outputs()` reads the `tool_result` blocks. It answers a non-streaming request with a whole message, though Copilot didn't send one.
- **The gate.** `tests/test_copilot_gate.py` parametrizes every case over both wires:
  - `openai` with `responses`, against `FakeResponsesApi`
  - `anthropic`, against `FakeMessagesApi`

  Each wire gets the same checks for every run kind: the exact offered set, refused `bash` and `read_mcp_resource` calls with no file written, the denied unallowed tool, the read-only-hint case, no token reaching the model, and the strace rules, with the remote-contact control from #296. The case count goes from 11 to about 21.
- **`check_env()`** accepts `anthropic` as well as `openai` and `azure`. The wire rule depends on the type:
  - `openai` and `azure` need `COPILOT_PROVIDER_WIRE_API=responses`, as now
  - `anthropic` must not set `COPILOT_PROVIDER_WIRE_API` at all, since that wire setting is for OpenAI-style providers and wasn't tested with `anthropic`

  So `COPILOT_PROVIDER_WIRE_API` becomes required for `openai` and `azure`, and refused for `anthropic`. The base URL is still `https://` only, and the other three provider names are still required.
- **Docs:** presets.md's copilot section gives an Anthropic example (`https://api.anthropic.com`, type `anthropic`, no wire setting). The CHANGELOG, and the threat-model row's "only the provider wire the gate checks", say both wires.

## Security's answer and condition

Security approved on 2026-10-21.

- **The wire setting:** accept `anthropic` only with `COPILOT_PROVIDER_WIRE_API` unset, and refuse it if it's listed, whatever its value. `check_env()`'s "every provider name set and listed" rule lets that one name be absent for `anthropic`.
- **Added condition, provider-side tools and betas.** Anthropic's API can run tools on its own side: web search, web fetch, code execution, and the MCP connector, which makes the API connect to a remote MCP server itself. They never touch the agent container, so strace can't see them. For every Anthropic request the gate asserts:
  1. every `tools` entry is a plain client tool, with a `name` and an `input_schema` and no server `type`, and the offered set is exactly the kind's tools
  2. there's no `mcp_servers` field, and no `container` or other server-execution field
  3. each `anthropic-beta` value is in a list pinned in the test, reviewed now, so a new beta in a later Copilot release fails the gate until someone looks at it
- **The Responses wire gets the same explicit check:** every tool is a `function` tool, never a hosted type such as `web_search`, `file_search`, `code_interpreter` or `mcp`, as well as the exact set.

## Risks and open questions

1. **Decided by security:** `COPILOT_PROVIDER_WIRE_API` is refused for `anthropic`.
2. **Copilot's `anthropic-beta` header** names a beta feature. Nothing in the gate depends on it, but a later release could change the beta, which only the pin and the gate catch, as for any Copilot change.
3. **The control PR #296** adds the remote-contact case. This builds on it, so #296 should merge first.

## Progress checklist

- [x] The lead and security approve this plan
- [x] Commit this plan and open the draft PR
- [x] `FakeMessagesApi`, with its own unit tests
- [x] The provider-side checks: plain client tools only, no `mcp_servers` or `container`, pinned `anthropic-beta` values, and no hosted tool types on the Responses wire
- [x] The gate parametrized over both wires
- [x] `check_env()` and its tests
- [x] presets.md, the threat-model row, the CHANGELOG
- [x] CodeRabbit once (one finding, fixed: the exact tool set is now checked on every request), `make check`, mark ready

## Validation

- The lab run above.
- The gate passed all 21 cases, both wires, in a container like CI's, with `JOTFLOW_REQUIRE_COPILOT=1` and strace.
- Unit tests show the provider-side checks fire on a hosted tool, a code-execution tool, `mcp_servers`, `container`, a tool with no schema, and an unreviewed beta.

## Handoff notes

None yet.
