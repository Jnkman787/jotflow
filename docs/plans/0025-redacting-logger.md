# Redacting logger

## Links and status

- Issue: #25
- Draft PR: #121
- Status: implementing
- Last verified: `make check` green, 1124 tests, 98.5% coverage; `logs.py` at 100%
- Next safe action: CodeRabbit or skip, CI on #121, mark ready

## Objective and done-when

Make it impossible for JotFlow's logs to carry Slack message text or a secret at the default level, whatever any library logs, and give developers an explicit, loud, local-only way to see message bodies when debugging.

The issue's boxes:

- [x] No log line at the default level contains message text or a token
- [x] Tests assert redaction on representative events

Refined:

- [x] Every line JotFlow writes passes through one redacting formatter. That includes tracebacks and stack text, so redaction applies to the final text, not only to the message format.
- [x] Token-shaped strings are always replaced, with or without the debug flag:
  - Slack tokens: `xoxb-`, `xoxp-`, `xoxa-`, `xoxe-`, `xoxs-`, `xapp-`
  - JotFlow run tokens: `jfr_`
  - GitHub tokens: `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`
  - `Authorization: Bearer` values
  - PEM private keys
- [x] Slack payload text fields are replaced in third-party lines: `"text"`, `'text'`, and `text=` in JSON, Python reprs, and form bodies. JotFlow's own modules never log text, as tests already enforce.
- [x] Third-party loggers stay capped at INFO, so library debug dumps of whole payloads never reach the output by default
- [x] A `--log-bodies` flag on `jotflow run` lifts the cap and the text redaction for local development:
  - it prints a loud warning at startup
  - it refuses to run inside JotFlow's image, when `JOTFLOW_IMAGE` is set, or in CI
  - tokens are still redacted with it on
- [x] The tests replay every built-in fixture thread through ingest and through library-style debug lines that carry the raw event, and assert that no message text and no token reaches the output at the default level

**Out of scope:** JSON-formatted logs; key-value lines stay. Shipping logs anywhere: there is no telemetry. The MCP audit line keeps its own content-free format.

## Design

**`src/jotflow/logs.py`:**

- `redact(text, *, bodies=False) -> str`: the token patterns always, and the payload text patterns unless `bodies`. Pure, so it is tested directly.
- `RedactingFormatter(logging.Formatter)`: formats the record, including `exc_info` and `stack_info`, and then redacts the whole string. Redacting at the formatter catches arguments, tracebacks, and any library's message, without trusting each call site.
- `configure_logging(level, *, bodies=False)`: one stream handler with the formatter, `force=True`. It caps `THIRD_PARTY_LOGGERS` at INFO unless `bodies` is on, and logs the warning when it is.
- `check_bodies_allowed(environ)`: refuses when `JOTFLOW_IMAGE` or `CI` is set, reusing the live check's notion of truthy.

**Call sites:** `commands/run.py` and `commands/check.py` import `configure_logging` from `logs.py`. `run.py` keeps a thin re-export so its tests and #55 do not break. `jotflow run` gains `--log-bodies`.

**Why the formatter and not a filter:** a filter sees the record before formatting. Arguments formatted with `%r`, and exception text, would still slip past it. The formatter sees the exact text that is written.

Alternatives considered: a structlog dependency, rejected because it adds a dependency for what one formatter does. Dropping all third-party lines, rejected because Slack connection errors at INFO and WARNING matter to operators.

## Risks and open questions

- The payload pattern cannot know every library's shape. The INFO cap is the main guard, and the text patterns are the second. A test that pushes real Slack-SDK-style payload reprs through the formatter keeps it honest.
- Over-redaction: a log line about a field named `text` loses its value. That is acceptable.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `logs.py`: `redact`, `RedactingFormatter`, `configure_logging`, the body flag guard
- [x] Wire `run` and `check`; `--log-bodies` on `run`
- [x] Tests: patterns, formatter with tracebacks, the fixture-thread replay, the flag guard and warning
- [x] Update documentation: README troubleshooting or logging note, architecture Data and privacy logging line
- [x] `make check` passes
- [ ] CodeRabbit local review, or skipped
- [ ] `closingIssuesReferences` checked, then mark the PR ready

## Validation

- `make check` passes: 1124 tests, 98.5% total coverage, and `logs.py` at 100%.
- `tests/test_logs.py`, 44 tests:
  - eight token shapes are redacted with and without `--log-bodies`: Slack bot, user, app, and refresh; JotFlow run; GitHub classic, fine-grained, and app
  - bearer values in headers and plain text are redacted, and so are RSA and PKCS#8 private keys
  - payload text is redacted in JSON, in nested JSON with escaped quotes, in Python reprs with either quote, in keyword reprs, and in form bodies, and kept with `--log-bodies`
  - ordinary lines pass unchanged: ingest lines, MCP audit lines, and words like `context` or `textual`
  - the formatter redacts exception text, tracebacks, and stack info
  - `configure_logging` installs the formatter and caps libraries at INFO even at debug. `--log-bodies` lifts the cap and warns, while tokens stay hidden.
  - `--log-bodies` is refused with `CI` or `JOTFLOW_IMAGE` set, and `jotflow run` exits 2 with the reason
  - every built-in fixture thread is replayed: library-style DEBUG and INFO lines carry each raw event, its JSON body, and a token, and ingest logs its decisions. No message text, no long word from it, and no token reaches the output at the default level.

## Handoff notes

- `configure_logging` and `THIRD_PARTY_LOGGERS` now live in `jotflow.logs`. `commands/run.py` re-exports `THIRD_PARTY_LOGGERS`, so existing imports keep working.
- New token formats go in `_SECRETS` in `logs.py`, with a line in the test's `SECRETS` table.
