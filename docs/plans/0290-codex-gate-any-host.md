# The codex gate fails if Codex contacts any host but the model

## Links and status

- Issue: #290 (M6), a follow-up to #284 (#289) and #220
- Status: built. Both tests, the control and the parser pass locally against the pinned Codex 0.163.0 and strace 6.8
- Last verified: 2026-10-21, against main

## Objective and done-when

#289 showed that Codex fetched OpenAI's model catalog on every run, and that the catalog sets each model's tools. Its test proves that one fetch is gone. #290 asks for a gate that fails on any other remote source too, such as feature flags or a provider list, not only the one we know about.

- [x] Every contact Codex's whole process tree attempts is checked, refused or not, so a fetch that fails and falls back quietly still fails the gate
- [x] It covers Codex as a deployment runs it: OpenAI's own provider at its default address
- [x] It fails, never skips, when `JOTFLOW_REQUIRE_CODEX=1`, as in CI's gate job
- [x] presets.md and the threat model's Codex row say the gate checks for any outbound host

## Why strace, and why two tests

**Blocking the network isn't enough.** A blocked fetch can fail quietly and fall back, and the run still passes. So the gate records attempts: Codex runs under `strace -f -ff`, tracing connect, sendto, sendmsg and sendmmsg, plus `io_uring_setup`, since a connect sent through io_uring is invisible to strace. `tests/outbound_trace.py` reads the trace. It allows loopback on the named ports, netlink, AF_UNSPEC, and a Unix socket that refused the connection, as glibc tries nscd first. Everything else is refused, including DNS and a Unix socket that connected, since a lookup service would hide the name asked for.

**A custom base address hides fetches.** Pointing OpenAI's own provider at the fake model with `openai_base_url` is close to a deployment. But Codex then skips the model catalog even with discovery forced on: I checked, and the run still passed. So a test that only uses a custom address can't see fetches Codex makes only for OpenAI's default address. Hence two tests:

1. **Behaviour, with the fake model** (written). The preset's config, OpenAI's own provider pointed at the fake model with `openai_base_url`, JotFlow's real MCP server and the fake GitHub server, for drafting and question:
   - every traced contact is the fake model or JotFlow's server on loopback
   - the fake model was asked only for `/v1/responses`: POST, plus the GETs of its WebSocket attempt, so no model list or other API call reached it
2. **Start-up, exactly as deployed** (to build). The preset's config with no override at all, in `unshare -rnm`, offline:
   - `/etc/nsswitch.conf` and `/etc/resolv.conf` are bind-mounted to `hosts: files dns` and `nameserver 127.0.0.1`, so every lookup is plain DNS, whatever the runner's own resolver setup
   - a small DNS listener on 127.0.0.1:53 inside the namespace records each name asked for and answers REFUSED
   - JotFlow's MCP server is turned off for this run. It's required, and unreachable offline it would stop Codex before the model is asked for, so nothing would be seen
   - the gate requires that the only name asked for is `api.openai.com`, the model, and that no contact goes anywhere but that listener
   - this sees everything before the first model answer, which can't arrive offline. Test 1 covers the run after it

**The control** runs test 2's setup with `features.api_key_model_discovery = true`, and must see `chatgpt.com` asked for. I've confirmed that locally, so the gate is shown to catch the catalog fetch it exists for.

## CI

The gate job installs `strace` from Ubuntu's archive with `apt-get install --no-install-recommends`, and already allows unprivileged user namespaces (#289). `unshare -m` needs no more than that.

## Risks

- **Flaky names.** If Codex looks up a second host legitimately, such as a telemetry host it then doesn't use, the gate fails. That's the intent: any new host should be a deliberate allow, named in the test.
- **strace on another architecture or kernel** may print a field differently. The parser fails closed on any line it can't read, and its unit tests use real lines from strace 6.8.
- **Test 2 stops at the first model call.** A fetch made only after a real answer, with the default address, is seen by neither test. Test 1 still sees its host if it isn't OpenAI's API.

## Progress checklist

- [x] The trace parser, with unit tests on real strace lines
- [x] Test 1, behaviour with the fake model, for drafting and question
- [x] The reviewer's look at this plan: process names in failures, exact loopback only, and the GitHub-server limit stated
- [x] Test 2, start-up exactly as deployed, with its control
- [x] CI installs strace; docs; `make check`
