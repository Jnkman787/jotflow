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
| `get_work` | The run's packet: new thread text plus the active topic index, or an approved proposal to execute |
| `propose_issue` | Structured draft: repo, title, body, labels, assignees, project, size |
| `propose_update` | Structured change to an existing issue: body edit, comment, label or assignee change |
| `link_message` | Associate a Slack message with a topic, or mark it as chatter |
| `ask_channel` | Post a clarifying question into the thread |
| `record_result` | Report the issue URL, or the error, after executing an approved proposal |

The same server is useful on its own: an engineer can point a terminal agent at it to query the whiteboard.

**Agent adapters.** Thin runners that launch the team's agent with the MCP server attached and a prompt, then return structured proposals.

- First adapter: a generic command template. Every major agent CLI has a headless mode and reads an MCP configuration, so a team selects their agent by editing one line. Ship tested presets for Claude Code, Codex CLI, Gemini CLI, and Copilot CLI.
- Later: an HTTP adapter and a reference SDK adapter.

**Agent output contract.** The agent acts only by calling the whiteboard MCP tools. Upshot records each `propose_*`, `link_message`, and `ask_channel` call as it happens; the agent's stdout is ignored. The prompt handed to the agent is minimal: a run id and the instruction to call `get_work` and then act. Each run receives a short-lived bearer token so no other local process can call the server.

**Execution runs.** Upshot holds no GitHub credentials. When a proposal is approved, or immediately in `auto` mode, Upshot launches the agent again with the approved proposal as its work packet and the instruction to perform the write using the GitHub access the team already gave it, typically GitHub's MCP server or the `gh` CLI, then call `record_result` with the issue URL. Upshot stores the URL, replies in the Slack thread, and posts the summary card. The agent may also use its GitHub access while drafting, for example to search for duplicates or to reference code and plan files.

## Approval gate

Each channel has a mode:

- `propose`: the bot posts a card showing repo, title, body summary, assignees, labels, project, and size, with Approve and Request Changes. Nothing is written until approved.
- `auto`: the bot writes immediately and posts the same summary card so anyone can see what was created and adjust.
- `paused`: the bot reads nothing and writes nothing.

The mode applies uniformly to creation, description edits, comments, assignment, and labels. Per-action granularity is deferred until someone needs it.

Who may approve is configurable: anyone in the channel by default, or an approver list mapped through identity links. A reaction emoji may optionally count as approval.

Upshot enforces the mode in its own flow: it never hands the agent a proposal to execute until the mode or a human allows it, and proposals are validated against the configured repo and label allowlists before a card is posted. Because the agent holds the team's GitHub credentials, a misbehaving or prompt-injected agent could still write on its own. Teams should scope the agent's GitHub access to the repositories they intend Upshot to touch.

## Correlation

This is the novel and risky piece. The agent, not the orchestrator, decides where a message belongs, but the orchestrator constrains the decision.

- Every packet includes the channel's active topic index.
- The agent classifies each new message as: new topic, belongs to topic X, or chatter.
- Below a confidence threshold the agent uses `ask_channel` rather than guessing.
- Humans override with a reaction or reply; overrides are recorded so the index stays correct.
- Before proposing a new issue the agent must search the target repository for duplicates with its own GitHub access and cite any candidates in the proposal.

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

The local store (SQLite by default) is a **rebuildable cache**. Each issue the agent creates carries the Slack permalinks it came from, because Upshot puts them in the proposal body; each linked Slack message receives a reply with the issue URL. The correlation map can be reconstructed from Slack alone. Slack's own retention policies therefore apply automatically.

Content leaves the team's control in exactly one place: the agent they chose. If that agent uses a hosted model, thread text goes to that vendor under the team's own agreement. Upshot sends only the thread in question, never channel history, and documents this clearly.

There is no hosted component and no telemetry.

## Permissions

| Component | Needs | Does not need |
|---|---|---|
| Upshot | A Slack app installed in the team's workspace: `channels:history`, `groups:history`, `chat:write`, `reactions:read`, `reactions:write`, `users:read`, `commands`, plus an app-level token for Socket Mode | Any GitHub access |
| Team's agent | Whatever GitHub access the team already gave it | Any Slack access |

Each team creates a dedicated Slack app for Upshot from the manifest in this repository, inside their own workspace. The bot's display name and icon are theirs to choose; "Upshot" is only the manifest default. Reusing an existing app is not supported: scopes, event subscriptions, and slash commands would have to be merged by hand and would collide with whatever the app already does. Slack requires two manual steps that no software can skip, creating the app and clicking Allow on the install screen, so `upshot init` wraps everything around them: it renders the manifest, opens the create-from-manifest link, validates the pasted tokens, and writes the environment file.

Upshot must be a Slack app because something has to be woken when a message is posted. Slack's MCP server is pull-only: an agent that is already running can read, search, or post through it, but nothing in it fires on a new message. Only an installed app can subscribe to events. The agent needs no Slack access because Upshot fetches thread text, hands it over through `get_work`, and does all posting itself.

## Configuration

Two tiers, because every team supplies every value and the two kinds change at different speeds.

| Tier | Lives in | Changed by | Holds |
|---|---|---|---|
| Policy | `upshot.toml`, deployed with the service | An admin editing the file | Channel to default repo mapping, repo and label allowlists, approver list, debounce window |
| Runtime | The local database | Slack slash commands | Channel mode (`propose`, `auto`, `paused`), Slack to GitHub identity links |

Setup is a first-class feature: `upshot init` asks a few questions and writes `upshot.toml` plus a `.env` template, and the Slack app manifest installs in a couple of clicks. A team should go from clone to connected bot without reading source.

## Identity

Assignment requires a Slack user to GitHub handle mapping. A self-service slash command lets each person link their own account. No admin config file is required.

## Security posture

- Channel content is untrusted input reaching an agent that holds GitHub credentials. Upshot limits what it asks for: proposals are validated against the configured repo and label allowlists before they can be approved, and the agent is only ever instructed to execute approved proposals. The team scopes the agent's GitHub token to the intended repositories.
- Issues and comments are created by the team's agent under whatever identity its GitHub credentials carry, with the proposing Slack user named in the body.
- No secrets in the repository. Configuration is by environment variables. A Slack app manifest is provided for installation. Upshot itself requires no GitHub credentials.
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
| GitHub | none | Upshot holds no GitHub credentials. The team's agent performs writes with its own tooling. |
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
  execution/      hands approved proposals back to the agent and records results
```

The major agent CLIs install through npm, so the container that runs the adapter needs Node present regardless of the orchestrator's language. For v1 a single image carries both. The agent runner can later move to its own container that reaches the MCP server over HTTP.
