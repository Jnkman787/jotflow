# AGENTS.md

Instructions for coding agents working in this repository: Claude Code, Codex CLI, Gemini CLI, Cursor, GitHub Copilot, or any other. Humans should read [CONTRIBUTING.md](CONTRIBUTING.md) first; this file is the detailed rulebook both follow.

## Scope

JotFlow is a self-hosted Slack app that exposes an MCP server. It watches a Slack channel, groups messages into topics, and has the team's own agent draft GitHub issues through JotFlow's tools. A human approves, then the agent performs the GitHub write with its own access.

Read before changing behaviour:

- [docs/architecture.md](docs/architecture.md): the design contract.
- [docs/roadmap.md](docs/roadmap.md): milestones and decisions D1 to D12.

## Principles that are not up for debate

- JotFlow's code never uses GitHub credentials. The team's agent performs every GitHub write, in its own container, and only for a proposal JotFlow has handed it after approval or in `auto` mode.
- The agent never sees JotFlow's secrets. Shipped presets give drafting runs, which read Slack text, only a read-only GitHub token and no shell or write tools; only execution runs, which receive approved structured fields, get the write token.
- The agent acts only through JotFlow's MCP tools. Its stdout is never parsed for results.
- JotFlow stores pointers, not message content. No column, log line, or file may hold a Slack message body.
- JotFlow never calls an LLM itself. The team supplies the agent.
- No hosted component and no telemetry.

A change that conflicts with one of these needs an architecture change agreed in an issue first.

## Layout

```
src/jotflow/
  models.py       shared contracts: proposals, runs, results, classifications, topics, channel modes
  config.py       environment-driven settings: secrets and process knobs
  policy.py       jotflow.toml schema and loader: channels, repos, labels, approvers
  cli.py          entry point
  orchestrator/   event ingest, debouncing, topic index, proposal lifecycle, approval cards
  whiteboard/     the MCP server
  adapters/       agent runners and presets
  execution/      execution runs: hands approved proposals back to the agent, checks results
tests/            pytest suite
docs/             architecture, roadmap, plans
```

## Commands

| Command | Does |
|---|---|
| `make install` | Create the environment with uv, including dev tools |
| `make check` | Lint, format check, type check, and tests: what CI runs |
| `make test` | Tests only |
| `make format` | Apply ruff formatting and safe fixes |
| `make run` | Run the `jotflow` CLI |

Run `make check` before pushing. CI runs the same checks on Python 3.12 and 3.13.

## Work tracking

GitHub Issues on this repository are the system of record. Do not track work anywhere else.

**Search before creating.** Before opening an issue, search open issues for one that already owns the outcome. Refine that issue instead of opening a parallel one. Open a new issue only for independently deliverable work with its own objective and acceptance criteria. Never open an issue just to mirror a PR or a minor step.

**Titles** take exactly one type prefix, in `type(scope): subject` or `type: subject` form:

| Prefix | Use for |
|---|---|
| `feat:` | New functionality |
| `fix:` | A bug |
| `docs:` | Documentation |
| `chore:` | Maintenance: dependencies, tooling, cleanup, tests |
| `ci:` | Workflows, builds, releases |
| `security:` | Security work of any kind |
| `task:` | Planned work that is not a code change |
| `epic:` | A parent issue that rolls up sub-issues |

The scope names a subsystem, usually matching a `component:*` label: `slack`, `mcp`, `agent`, `store`, `cli`, `infra`, `docs`.

**Every issue** has one `component:*` label, its milestone, and an assignee. Work items are attached to their milestone's epic as native sub-issues, not listed in the epic's body.

## Plan-first delivery

For any non-trivial issue:

1. Confirm the issue is ready: a bounded objective, testable done-when criteria, and no unresolved decision. If a decision is open, ask on the issue instead of starting.
2. Create a branch named `<type>/<short-topic>`, for example `feat/slack-app-manifest`.
3. Copy [docs/plans/TEMPLATE.md](docs/plans/TEMPLATE.md) to `docs/plans/<short-topic>.md` and fill it in. The plan links its issue and any relevant roadmap or architecture sections.
4. Commit the plan before any implementation and open a draft PR that links the issue.
5. Work through the plan's checklist in order. Record decisions, deviations, and validation results in the plan as they happen.
6. At any interruption or handoff, update the plan's status and next safe action so another agent can resume without this conversation.
7. When done, set the plan's status to `completed` and mark the PR ready for review.

The plan stays in the repository after merge as the record of what was built and why. If the implementation diverges from the plan, update the plan in the same PR.

## Commits and pull requests

- Commit messages and PR titles follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `refactor:`, `test:`, `security:`, with an optional scope. CI rejects a PR whose title does not match.
- A PR title describes the change it delivers, which may differ from the issue's type.
- Use `Fixes #N` only in the PR that completes issue N. Use `Refs #N` for partial work, epics, and issues delivered across several PRs.
- One concern per PR. Design changes go in `docs/architecture.md` or `docs/roadmap.md` in the same PR as the code that depends on them.
- Squash merge is the only merge method on `main`. Force pushes to `main` are blocked.
- Do not add AI attribution to commits, PRs, or issues: no `Co-Authored-By` trailers naming an agent, no "Generated with" footers.

## Testing

- New behaviour comes with tests. Bug fixes come with a test that fails before the fix.
- Tests must not need network access, a Slack workspace, or GitHub. Use the replay fixtures and fakes.
- Do not weaken a test or a type annotation to make CI pass.

## Documentation

- Update docs in the same PR as any change to behaviour, configuration, commands, or interfaces.
- Architecture, deployment, and key workflow changes are drawn as Mermaid diagrams in Markdown, not images.

## Security

- Treat Slack content as untrusted input. Never let it reach a shell command, a file path, or an instruction to the agent without validation.
- No secrets, tokens, or workspace identifiers in the repository. Configuration comes from the environment.
- Workflows use the minimum token permissions. Never use `pull_request_target` to check out code from a fork.

## Never commit

- `.env` or any file containing tokens or private keys
- Local databases: `*.db`, `*.sqlite`
- Agent CLI local settings and caches, such as `.claude/settings.local.json`
- Editor and OS files
