# Enforce an 80% unit test coverage floor

## Links and status

- Issue: #76
- Draft PR: #78
- Status: completed
- Last verified: `make check` green locally on Python 3.12, 243 tests, 95.74% total coverage
- Next safe action: review and merge #78

## Objective and done-when

Measure line and branch coverage on every run of the tests, and fail locally and in CI when total coverage drops below 80%.

Done when:

- [x] `pytest-cov` in the dev dependency group; coverage config in `pyproject.toml` with branch coverage and `fail_under = 80`
- [x] `make cov` prints the per-file report with missing lines; `make check` enforces the floor
- [x] CI runs tests with coverage and fails below 80%, on Python 3.12, 3.13, and 3.14
- [x] `src/jotflow/commands/run.py` is above 80% using fakes, with no network
- [x] AGENTS.md states the floor and that new code comes with tests that keep it

**Out of scope:** per-file floors, coverage upload services or badges, and raising coverage in files other than `commands/run.py`.

## Design

- **Dependency.** `pytest-cov>=6.0` in the `dev` group, locked at 7.1.0 with `uv add --dev`. It is the only new dependency, and the issue names it.
- **Config** in `pyproject.toml`:
  - `[tool.coverage.run]`: `source_pkgs = ["jotflow"]`, `branch = true`. `source_pkgs` measures the installed package by name, so the src layout needs no path tricks.
  - `[tool.coverage.report]`: `fail_under = 80`, `show_missing = true`, and `exclude_also` for `if __name__ == "__main__":`.
  - The floor lives only in this config. `make` and CI both read it, so it changes in one place.
- **Make.** A new `cov` target runs `pytest --cov --cov-report=term-missing`. `check` runs `cov` instead of `test`. `test` stays a fast run without coverage.
- **CI.** The `Test` step in the `check` matrix runs the same command as `make cov`, on all three Python versions. A run below the floor exits non-zero, which fails the job.
- **`commands/run.py`.** The uncovered part is `serve()`: building the Bolt app, startup checks, connecting, waiting, and the error exits. The tests replace `AsyncApp`, `AsyncSocketModeHandler`, and `install_stop_signals` in the module with fakes. The real `fetch_identity` and `missing_channels` run against a fake Slack client. The tests then drive each path:
  - a clean run stopped by the stop event
  - a Slack setup error exiting 1
  - Slack refusing the Socket Mode connection exiting 1 and closing the handler
  - a network error exiting 1 and closing the handler
  - Ctrl-C exiting 130

  No test opens a socket.
- **AGENTS.md.** Under Testing: the 80% line and branch floor, enforced by `make check` and CI, and that new code comes with tests that keep it.

Alternatives considered: putting `--cov` in pytest's `addopts` would slow every `pytest` run and break running one test file in an editor. `--cov-fail-under` on the command line would duplicate the number in two places.

## Risks and open questions

- Total coverage was measured at 94%, so the floor passes today. It should not be raised in this PR.
- #77 renames plan files in parallel. This plan uses the new `NNNN-topic.md` name and touches no other plan file.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Add `pytest-cov` and the coverage config
- [x] `make cov`, and `make check` enforcing the floor
- [x] CI test step with coverage
- [x] Tests for `serve()` in `commands/run.py`
- [x] Update documentation: AGENTS.md Testing and Commands
- [x] `make check` passes
- [x] Local CodeRabbit review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make cov`: 243 tests pass. Total line and branch coverage is 95.74%, and the report ends "Required test coverage of 80.0% reached", which shows the floor comes from `pyproject.toml`.
- `commands/run.py`: 100%, up from 75%. Six new tests drive `serve()` with fakes, with no network:
  - a clean run that connects, waits, and closes
  - channels to invite
  - a setup error before connecting
  - a refused Socket Mode connection
  - an unreachable Slack on connect
  - Ctrl-C before signal handlers are installed
- The floor fails runs below it:
  - running only `tests/test_cli.py` with coverage reports "Required test coverage of 80.0% not reached. Total coverage: 33.42%" and exits 1
  - `--cov-fail-under=99` on the full suite exits 1
- `make check` passes, including `make eol`. Every changed file is LF in the index and the working tree.
- CodeRabbit (local CLI, `--committed --base main -c AGENTS.md`): 0 findings.
- Lowest files now: `commands/init.py` at 91%, then `envfile.py` and `slack_auth.py` at 96%. Raising them is out of scope.


## Handoff notes

Nothing pending. `make test` stays a quick run without coverage for local iteration; `make check` and CI are what enforce the floor.
