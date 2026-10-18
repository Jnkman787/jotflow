# Upshot architecture

This document records the design decisions made before the first line of code. It is the contract the implementation is built against. Change it through a pull request when a decision changes. The build order, milestone definitions, and the numbered decisions D1 to D4 are in [roadmap.md](roadmap.md).

## Problem

A small team wants anyone, including people without a coding agent or GitHub fluency, to propose work by writing in a shared Slack channel. An AI agent should turn those conversations into well-formed GitHub issues, keep the issues current as the conversation continues, and keep the whole team informed without a separate "I filed this" message. The team supplies its own agent. The tool must be usable by any team, with any agent, on their own infrastructure.

## Shape

Upshot is an **application with an MCP surface**, not an MCP server on its own and not a framework-specific agent.

An MCP server cannot initiate anything; it answers requests from an agent that is already running. Upshot is event-driven: Slack produces events, something must be listening, hold state across days, debounce bursts, and decide when to invoke an agent. That something is the orchestrator. Tying the project to one agent framework would contradict the goal of working with any agent, so the orchestrator is framework-free and exposes its capabilities over MCP, which every major agent CLI and SDK already speaks.

### Components

**Orchestrator.** A long-running service.

- Subscribes to Slack events for configured channels (Socket Mode by default, Events API optional).
- Debounces: a thread or loose message is not acted on until it has been quiet for a configurable window, so the first draft already reflects a burst of replies.
- Maintains the **topic index**: a per-channel set of active topics within a rolling window, each with a short generated title and its linked repo and issue number.
- Builds the agent packet: the relevant thread text fetched live from Slack, plus the compact topic index.
- Invokes the configured agent adapter and collects its structured proposals.
- Renders Block Kit proposal cards and handles Approve, Request Changes, and override reactions.
- Executes approved proposals against GitHub.

**Whiteboard MCP server.** Served by the orchestrator process over Streamable HTTP. Tools, subject to change:

| Tool | Purpose |
|---|---|
| `list_topics` | Active topics for a channel with ids, titles, linked issues |
| `get_topic` | A topic's linked Slack messages and issue state |
| `search_issues` | Search the target repo for likely duplicates |
| `propose_issue` | Structured draft: repo, title, body, labels, assignees, project, size |
| `propose_update` | Structured change to an existing issue: body edit, comment, label or assignee change |
| `link_message` | Associate a Slack message with a topic, or mark it as chatter |
| `ask_channel` | Post a clarifying question into the thread |

The same server is useful on its own: an engineer can point a terminal agent at it to query the whiteboard.

**Agent adapters.** Thin runners that launch the team's agent with the MCP server attached and a prompt, then return structured proposals.

- First adapter: a generic command template. Every major agent CLI has a headless mode and reads an MCP configuration, so a team selects their agent by editing one line. Ship tested presets for Claude Code, Codex CLI, Gemini CLI, and Copilot CLI.
- Later: an HTTP adapter and a reference SDK adapter.

**Agent output contract.** The agent acts only by calling the whiteboard MCP tools. Upshot records each `propose_*`, `link_message`, and `ask_channel` call as it happens; the agent's stdout is ignored. The prompt handed to the agent is minimal: a run id and the instruction to call `get_work` and then act. Each run receives a short-lived bearer token so no other local process can call the server.

**GitHub writer.** A GitHub App the team installs on the repositories they choose. All writes go through it. The agent never holds GitHub write credentials. The agent may be given read-only GitHub access by the team if they want richer drafts that reference code or plan files.

## Approval gate

Each channel has a mode:

- `propose`: the bot posts a card showing repo, title, body summary, assignees, labels, project, and size, with Approve and Request Changes. Nothing is written until approved.
- `auto`: the bot writes immediately and posts the same summary card so anyone can see what was created and adjust.
- `paused`: the bot reads nothing and writes nothing.

The mode applies uniformly to creation, description edits, comments, assignment, and labels. Per-action granularity is deferred until someone needs it.

Who may approve is configurable: anyone in the channel by default, or an approver list mapped through identity links. A reaction emoji may optionally count as approval.

Because the orchestrator is the only component with write access, the mode is a rule enforced in code, not a request in a prompt.

## Correlation

This is the novel and risky piece. The agent, not the orchestrator, decides where a message belongs, but the orchestrator constrains the decision.

- Every packet includes the channel's active topic index.
- The agent classifies each new message as: new topic, belongs to topic X, or chatter.
- Below a confidence threshold the agent uses `ask_channel` rather than guessing.
- Humans override with a reaction or reply; overrides are recorded so the index stays correct.
- Before proposing a new issue the agent must call `search_issues` for duplicates.

## Data and privacy

Upshot stores pointers, not content.

Stored:

- channel id, message timestamp, thread timestamp, mapped to a topic id
- topic id mapped to a repo and issue number, plus a short generated title
- proposal state: pending, approved, rejected, applied
- Slack-to-GitHub identity links and per-channel settings

Not stored:

- message bodies
- channel history
- agent transcripts

Thread text is fetched from Slack when building a packet, passed to the agent, and discarded. Logs redact message bodies by default.

The local store (SQLite by default) is a **rebuildable cache**. Each issue created by Upshot carries the Slack permalinks it came from; each linked Slack message receives a reply with the issue URL. The correlation map can be reconstructed from Slack and GitHub alone. Slack's own retention policies therefore apply automatically.

Content leaves the team's control in exactly one place: the agent they chose. If that agent uses a hosted model, thread text goes to that vendor under the team's own agreement. Upshot sends only the thread in question, never channel history, and documents this clearly.

There is no hosted component and no telemetry.

## Configuration

Two tiers, because every team supplies every value and the two kinds change at different speeds.

| Tier | Lives in | Changed by | Holds |
|---|---|---|---|
| Policy | `upshot.toml`, deployed with the service | An admin editing the file | Channel to default repo mapping, repo and label allowlists, approver list, debounce window |
| Runtime | The local database | Slack slash commands | Channel mode (`propose`, `auto`, `paused`), Slack to GitHub identity links |

Setup is a first-class feature: `upshot init` asks a few questions and writes `upshot.toml` plus a `.env` template, and the Slack and GitHub App manifests install in a couple of clicks. A team should go from clone to connected bot without reading source.

## Identity

Assignment requires a Slack user to GitHub handle mapping. A self-service slash command lets each person link their own account. No admin config file is required.

## Security posture

- Channel content is untrusted input reaching an agent. Only the orchestrator writes to GitHub, against a fixed allowlist of repos, labels, and projects.
- Issues and comments are attributed to the Upshot GitHub App, with the proposing Slack user named in the body.
- No secrets in the repository. Configuration is by environment variables. Slack and GitHub App manifests are provided for installation.
- Continuous integration runs on forked pull requests with a read-only token. The `pull_request_target` trigger is never used to check out fork code.

## Deferred

- Two-way sync from GitHub comments back into Slack threads. Easy with webhooks, but held until the forward path is solid.
- Per-action approval granularity.
- Other chat platforms. The orchestrator's Slack dependency should stay behind an interface so this is possible later.

## Stack

Python 3.12 or newer.

| Concern | Choice | Why |
|---|---|---|
| Slack | slack-bolt (AsyncApp, Socket Mode) | Maintained by Slack. Socket Mode means no public URL for self-hosted installs. |
| MCP | the official `mcp` package, FastMCP server over Streamable HTTP | Tool schemas derive from the same Pydantic models used everywhere else. |
| GitHub | githubkit | Async, typed, first-class GitHub App installation auth. |
| State | SQLite via aiosqlite | Pointers only. Postgres stays possible behind the same interface. |
| Tooling | uv, ruff, mypy strict, pytest | Locked dependencies, one formatter, typed contracts. |

The Pydantic models in `src/upshot/models.py` are the single source of truth for the MCP tool contract, the validation applied before any GitHub write, and the shape rendered into Slack approval cards.

Package layout:

```
src/upshot/
  models.py       shared contracts: proposals, classifications, topics, channel modes
  config.py       environment-driven settings, no secrets in the repo
  cli.py          entry point
  orchestrator/   event ingest, debouncing, topic index, proposal lifecycle, approval cards
  whiteboard/     the MCP server
  adapters/       agent runners; generic command template first
  github/         GitHub App writer with allowlists
```

The major agent CLIs install through npm, so the container that runs the adapter needs Node present regardless of the orchestrator's language. For v1 a single image carries both. The agent runner can later move to its own container that reaches the MCP server over HTTP.
