# Gate the copilot preset on Anthropic's API too

## Links and status

- Issue: #288 (M6), a follow-up to #59 and #285
- Draft PR: to be opened
- Status: planning. The lead and security review this plan before any code
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

## Risks and open questions

1. **For security:** is refusing `COPILOT_PROVIDER_WIRE_API` for `anthropic` right, rather than ignoring it? Refusing keeps the run to exactly what the gate tested.
2. **Copilot's `anthropic-beta` header** names a beta feature. Nothing in the gate depends on it, but a later release could change the beta, which only the pin and the gate catch, as for any Copilot change.
3. **The control PR #296** adds the remote-contact case. This builds on it, so #296 should merge first.

## Progress checklist

- [ ] The lead and security approve this plan
- [ ] Commit this plan and open the draft PR
- [ ] `FakeMessagesApi`, with its own unit tests
- [ ] The gate parametrized over both wires
- [ ] `check_env()` and its tests
- [ ] presets.md, the threat-model row, the CHANGELOG
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- The lab run above.
- Once built: the gate in CI, and `make check`.

## Handoff notes

None yet.
