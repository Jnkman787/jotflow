# Upshot

**Turn team chat into GitHub issues.**

Upshot is a self-hosted, agent-agnostic bridge between a shared Slack channel and your GitHub repositories. Anyone on the team, engineer or not, drops an idea, bug, or request into the channel. Upshot watches the conversation, correlates follow-up messages and replies to the same topic, and has *your* AI agent draft the corresponding GitHub issue. A human approves it, or the channel is configured to let it through automatically. Either way, the result is posted back to Slack so everyone sees what was created.

> **Status: design phase.** There is no runnable code yet. The architecture and principles below are the contract the first implementation will be built against. See [docs/architecture.md](docs/architecture.md) for the full design.

## Why

- Agents can now generate issues faster than a small team can track them. People still end up pinging each other on Slack to say "I filed this, go look."
- Non-engineers rarely have a coding agent in a terminal. A Slack channel is something they already use.
- GitHub's own Copilot-in-Slack integration does part of this, but it is closed source, only acts when mentioned, and requires a Copilot Business or Enterprise plan. Upshot is free, open, runs on your infrastructure, and works with whatever agent your team already has.

## How it works

```
Slack channel  ──events──▶  Upshot orchestrator  ──MCP──▶  your agent (Claude Code, Codex, Gemini CLI, ...)
      ▲                              ▲                              │             │
      │                              └──── proposals, results ──────┘             │ your agent's own GitHub access
      └── approval cards, replies                                                 ▼
                                                                          issues, comments, labels, assignees
```

1. **Orchestrator.** A long-running service that subscribes to Slack events, debounces a burst of messages until the thread goes quiet, and hands the agent a compact packet: the new messages plus an index of currently active topics.
2. **Whiteboard MCP server.** The orchestrator exposes its state and actions as Model Context Protocol tools: list active topics, read a topic's history, search for duplicate issues, propose an issue, propose an update, link a message to a topic, ask the channel a question. Any MCP-capable agent can drive it.
3. **Agent adapters.** Thin runners that launch the team's agent in headless mode with the MCP server attached. The first adapter is a generic command template with presets for the major agent CLIs.
4. **Approval gate.** Each channel runs in `propose` mode (the bot posts a card with repo, title, assignees, labels, and size, and waits for Approve or Request Changes) or `auto` mode (the write happens immediately and a summary card follows). Once a proposal is approved, Upshot hands it back to your agent, which creates or updates the issue with its own GitHub access and reports the result. The mode applies to the full lifecycle: creation, description edits, comments, assignment, and labels.

## What a team needs

| Component | Needs | Does not need |
|---|---|---|
| Upshot | A Slack app in your workspace, created from the included manifest: read channel history, post messages, reactions, slash commands, Socket Mode | Any GitHub access |
| Your agent | Whatever GitHub access you already gave it, for example GitHub's MCP server or the `gh` CLI | Any Slack access |

## Principles

- **Bring your own agent.** Upshot never calls an LLM itself. You supply the agent and its credentials.
- **Upshot holds no GitHub credentials.** Your agent emits structured proposals through Upshot's tools. Once a proposal is approved, Upshot hands it back to your agent, which performs the write with the GitHub access you already gave it and reports the issue URL back. Upshot never asks the agent to write anything a human has not approved, unless the channel is in `auto` mode.
- **Store pointers, not messages.** Upshot keeps Slack message timestamps, topic links, issue numbers, and proposal state. It does not store message bodies. Thread text is fetched from Slack on demand, passed to the agent, and discarded. Logs redact message content by default.
- **The local store is a rebuildable cache.** Every issue carries the Slack permalinks it came from; every linked Slack message gets a reply with the issue URL. Delete the database and the correlation map can be rebuilt from Slack and GitHub.
- **No hosted component, no telemetry.** Nothing phones home.

## Roadmap

The plan lives in [docs/roadmap.md](docs/roadmap.md): five milestones from "installable" to "ready for another team," the decisions each rests on, and what done means for each. Work is tracked as GitHub milestones and issues on this repo.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Security concerns go through [SECURITY.md](SECURITY.md), not public issues.

## License

[Apache-2.0](LICENSE)

Upshot is an independent open source project. It is not affiliated with or endorsed by Slack Technologies or GitHub, Inc.
