# Contributing to Upshot

Thanks for your interest. Upshot is in its design phase, so the most useful contributions right now are design feedback and discussion on the architecture in `docs/architecture.md`.

## Ground rules

- All changes land through pull requests. Direct pushes to `main` are blocked.
- Pull requests require review from a code owner before merging. Maintainers listed in `.github/CODEOWNERS` are the only people who can merge.
- Keep pull requests focused. One concern per PR.
- Design changes go through `docs/architecture.md` in the same PR as the code that depends on them.

## Before opening a PR

- Open an issue first for anything beyond a small fix so the approach can be agreed on.
- Do not commit secrets, tokens, or workspace identifiers. Configuration is by environment variables only.
- Never add a workflow that uses `pull_request_target` to check out fork code.

## Principles that are not up for debate

These are in the README and architecture document. Contributions that violate them will not be merged:

- The orchestrator is the only component that writes to GitHub.
- Upshot stores pointers, not message content.
- Upshot never calls an LLM itself; the team supplies the agent.
- No hosted component, no telemetry.

## Licensing

By contributing you agree that your contributions are licensed under the Apache License 2.0 that covers the project.
