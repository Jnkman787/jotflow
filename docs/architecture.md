# JotFlow architecture

This document is the design contract the implementation is built against. Change it through a pull request when a decision changes. The build order, milestone definitions, and the numbered decisions D1 to D12 are in [roadmap.md](roadmap.md).

## Problem

A small team wants anyone, including people without a coding agent or GitHub fluency, to propose work by writing in a shared Slack channel. An AI agent should turn those conversations into well-formed GitHub issues, keep the issues current as the conversation continues, and keep the whole team informed without a separate "I filed this" message. The team supplies its own agent. The tool must be usable by any team, with any agent, on their own infrastructure.

## Shape

JotFlow is an **application with an MCP surface**, not an MCP server on its own and not a framework-specific agent.

An MCP server cannot initiate anything; it answers requests from an agent that is already running. JotFlow is event-driven: Slack produces events, something must be listening, hold state across days, debounce bursts, and decide when to invoke an agent. That something is the orchestrator. Tying the project to one agent framework would contradict the goal of working with any agent, so the orchestrator is framework-free and exposes its capabilities over MCP, which every major agent CLI and SDK already speaks.

### Components

**Orchestrator.** A long-running service.

- Subscribes to Slack events for configured channels over Socket Mode. Ignores its own messages and drops events Slack delivers twice.
- Parses intent keywords at the start of messages and applies the ones JotFlow handles itself (see Intent keywords).
- Debounces: a thread or loose message is not acted on until it has been quiet for a configurable window, so the first draft already reflects a burst of replies.
- Maintains the **topic index**: a per-channel set of active topics within a rolling window, each with a short generated title and its linked repo and issue number.
- Records every agent run and enforces one run per thread at a time (see Run lifecycle).
- Builds the agent packet: the relevant thread text fetched live from Slack, the compact topic index, and short excerpts of likely-related threads.
- Launches the agent through the configured adapter and records the tool calls it makes.
- Validates proposals, renders Block Kit proposal cards, and handles Approve, Request Changes, and reactions.
- After approval, or immediately in `auto` mode, launches an execution run in which the agent performs the GitHub write.

**Whiteboard MCP server.** Served by the orchestrator process over Streamable HTTP. Tools, subject to change:

| Tool | Purpose |
|---|---|
| `list_topics` | Active topics for a channel with ids, titles, linked issues |
| `get_topic` | A topic's linked Slack messages and issue state |
| `get_work` | The run's packet: new thread text, the active topic index, and related-thread excerpts for a drafting run; only the approved structured proposal for an execution run |
| `propose_issue` | Structured draft: repo, title, body, labels, assignees, size |
| `propose_update` | Structured change to an existing issue: body edit, comment, label or assignee change |
| `link_message` | Associate a Slack message with a topic, or mark it as chatter |
| `ask_channel` | Post a clarifying question into the source thread only |
| `record_result` | Report the issue URL, or the error, after executing an approved proposal |

Every call carries a token, and what a token may do depends on who holds it:

| Token | Issued to | May call |
|---|---|---|
| Drafting run | One drafting run, one channel | `get_work`, `list_topics`, `get_topic`, `propose_*`, `link_message`, `ask_channel` |
| Execution run | One execution run, one approved proposal | `get_work`, `record_result` |
| Operator (M5, D12) | A person, created and revoked by an admin, named channels only | `list_topics`, `get_topic` |

Tokens are short-lived for runs and revocable for operators. The server validates `Host` and `Origin` headers so a web page cannot reach it from a browser.

**Agent adapters.** Thin runners that launch the team's agent with the MCP server attached and a prompt.

- First adapter: a generic command template. Every major agent CLI has a headless mode and reads an MCP configuration, so a team selects their agent by editing one line. Presets ship for Claude Code and Codex CLI first, then Gemini CLI and Copilot CLI.
- A preset defines, per run kind, which of the CLI's tools are allowed. Drafting runs get no shell and no file-write tools, and only the team's read-only GitHub token. Execution runs get the write token, used to create or update one issue. Presets disable session transcripts where the CLI supports it.
- Later: an HTTP adapter and a reference SDK adapter.

**Agent output contract.** The agent acts only by calling the whiteboard MCP tools. JotFlow records each call as it happens; the agent's stdout is ignored. The prompt handed to the agent is minimal: a run id and the instruction to call `get_work` and then act.

**Execution runs.** JotFlow's code never uses GitHub credentials. When a proposal is approved, or immediately in `auto` mode, JotFlow launches the agent again. Its work packet contains only the approved structured fields, never raw Slack text. The agent first searches the target repo for the proposal's hidden marker, so a retry never creates a second issue, then performs the write with the GitHub access the team gave it and calls `record_result` with the issue URL. JotFlow checks that the URL points at the approved repo, stores it, replies in the Slack thread, and updates the card. A team may configure a cheaper model for execution runs, since they make a single write.

## Deployment

JotFlow and the agent run in **two containers**, started together by one Compose file (D6).

```mermaid
flowchart LR
    subgraph c1["JotFlow container"]
        orch["Orchestrator + MCP server"]
        db[("SQLite<br/>pointers and run records")]
        sec["Slack tokens"]
    end
    subgraph c2["Agent container"]
        agent["Team's agent CLI"]
        ghtok["Agent's GitHub access"]
    end
    slack["Slack"] <--> orch
    orch -- "launches runs" --> agent
    agent -- "MCP over HTTP, per-run token" --> orch
    agent --> gh["GitHub"]
```

The agent container receives an explicit allowlist of environment variables. It never sees JotFlow's `.env`, Slack tokens, or database. JotFlow's container never holds the agent's GitHub token. A prompt-injected agent therefore cannot reach JotFlow's Slack tokens. It can still misuse its own GitHub access within its run kind's tool allowlist; in particular, read-only access during drafting can copy private repo content into a proposal body or a clarifying question in Slack. Channel repo mappings and the agent's read token should cover only repos whose content may be discussed in that channel.

The team decides where both containers run. The only requirements are that JotFlow can start runs in the agent container and the agent can reach JotFlow's MCP port. A split across machines is the post-v1 HTTP adapter.

## Intent keywords

People never need syntax: a plain message is classified by the agent. Keywords let them steer explicitly. A keyword is the first word of a message followed by a colon (D10).

| Keyword | Handled by | Effect |
|---|---|---|
| `idea:`, `bug:` | Agent, as a hint | Suggests the kind of issue. The agent may overrule it; the card says when it does. |
| `re:` | JotFlow | Attaches the message to an existing topic or issue in this channel's repos. `re: 42` when unambiguous, `re: web#42` otherwise. |
| `skip:` | JotFlow | Marks the message as not an idea. Its text is kept out of agent packets. On a top-level message it covers the whole thread. |
| `ask:` (M5) | Question run | Read-only question answered in the thread from this channel's topics and repos (D12). |

Post-v1: `hold:` and `go:` to lengthen or shorten the wait before drafting, and `new:` to split a reply off into its own topic.

Rules:

- The colon is required, so sentences like "Skip button is broken" are never read as commands. Typos are tolerated only on keywords of four or more letters.
- JotFlow confirms a recognised keyword with a reaction on the message, so the poster knows it was understood. Reactions never restart the wait before drafting.
- Every keyword has an equivalent reaction, for people who forget or prefer clicking. Reactions from guests and external users are ignored.
- JotFlow strips the keyword before the agent sees the text, and passes the rest as untrusted thread content. A keyword never adds instructions to the agent's prompt.
- No keyword can skip approval or change the channel mode.
- Acting on someone else's message: with approvers off, anyone may, and JotFlow posts a visible note in the thread that the author can undo. With approvers on, only the author or an approver may.
- Editing a message after its card is posted never changes what is approved.
- `/jotflow help` shows the set privately, JotFlow pins a short help message when it joins a channel, and a newcomer's first card carries a one-line keyword tip.
- When JotFlow replies with an issue link, it includes the exact handle to use, for example "reply `re: web#42` to add to this", so nobody needs to know issue numbers.

## Channels and repos

Each channel lists the repos it may feed, each with a nickname and a one-line description of what it covers (D11). The agent picks the repo by matching the thread against those descriptions. The card shows the chosen repo prominently with a dropdown to change it before approving. When the agent is unsure, it asks in the thread. A channel with one repo needs none of this.

## Approval gate

Each channel has a mode:

- `propose`: the bot posts a card with Approve and Request Changes. Nothing is written until approved.
- `auto`: the write happens immediately and the same card follows, so anyone can see what was created and adjust.
- `paused`: the bot reads nothing and writes nothing.

The mode applies uniformly to creation, description edits, comments, assignment, and labels.

**What approval guarantees, always** (D7):

- The card shows the full issue body, the target repo, labels, assignees, size, and confidence. A public target repo shows a warning.
- Approving locks that exact content by hash. The execution run receives only those approved fields.
- The card records who approved and when.
- Guests and external Slack Connect users can never approve.

**Who may approve, configurable** (D7):

| Setting | Approve | Change mode | Self-approval |
|---|---|---|---|
| Approvers off (default) | Anyone in the channel | Anyone in the channel | Allowed |
| Approvers on | Listed approvers | Listed approvers | Not allowed |

Every mode change is announced in the channel. Approval by reaction is off by default and can be turned on.

**Request Changes** opens a dialog whose text JotFlow posts as a thread reply attributed to the person, so the next drafting run treats it like any other message (D9).

**What the gate is and is not.** JotFlow enforces the gate in its own flow: it never hands the agent a proposal to execute until the mode or a person allows it, and it validates proposals against the configured repos and label allowlists before posting a card. It is not a credential boundary: the agent holds the team's GitHub access, so a misbehaving agent could still write on its own. D6 narrows this: drafting runs, the ones exposed to raw Slack text, get read-only GitHub access. Teams should still scope the agent's write token to the repositories JotFlow is meant to touch.

**Open mode, stated plainly.** With approvers off, the default, the gate catches agent mistakes but not a single member acting alone: anyone can post text, approve it themselves, or switch the channel to `auto`. In `auto` mode, text from anyone in the channel reaches GitHub with no human review. That fits a small team of equals. Channels with wide membership, guests, or external Slack Connect users should turn approvers on. JotFlow warns in the channel when `auto` is set in a channel that has guests or external members.

## Conversation rules

Defaults that need no syntax (D9):

- A loose top-level message starts a candidate topic. The agent may attach it to an existing one.
- A reply in a thread whose topic has expired revives that topic.
- A follow-up reply in a thread with an issue produces an update proposal, gated like any other.
- The agent sees the thread in question plus short excerpts of likely-related threads, so it can correlate on content rather than titles alone.

## Correlation

This is the novel and risky piece. The agent, not the orchestrator, decides where a message belongs, but the orchestrator constrains the decision.

- Every packet includes the channel's active topic index and related-thread excerpts.
- The agent classifies each new message as: new topic, belongs to topic X, or chatter, and records any keyword hint it overruled and why.
- Below a confidence threshold the agent uses `ask_channel` rather than guessing.
- People override with `re:`, `skip:`, or the equivalent reaction; overrides are recorded so the index stays correct.
- Before proposing a new issue the agent searches the target repository for duplicates with its read-only GitHub access and cites any candidates in the proposal.

## Run lifecycle

Every agent run is recorded in the team's own database (D8).

- A run has a kind (drafting, execution, and from M5 question), a status, a deadline, and its linked proposal.
- At most one run per thread at a time. Messages arriving during a run trigger a fresh run afterwards, and the newer proposal supersedes the older pending one.
- A total concurrency cap limits how many agent processes run at once.
- On restart, interrupted drafting runs are re-queued and interrupted execution runs are reconciled: the next attempt checks for the proposal's hidden marker before writing.
- Proposal states: pending, approved, executing, applied, rejected, superseded, failed.

## Data and privacy

JotFlow stores pointers and derived metadata, never Slack message bodies.

Stored, in the team's own database:

- channel id, message timestamp, thread timestamp, mapped to a topic id
- topic id mapped to a repo and issue number, plus a short generated title
- proposals: structured fields, state, approver, content hash
- run records, identity links, and per-channel settings

Not stored by JotFlow: message bodies, channel history.

Thread text is fetched from Slack when building a packet, passed to the agent, and discarded. Logs redact message bodies by default.

The local store is a **rebuildable cache**. Each linked Slack message receives a reply with the issue URL, and by default each issue carries the Slack permalinks it came from, so the correlation map can be reconstructed from Slack alone. Setting `issue_links = "none"` keeps permalinks and every other workspace detail out of issue bodies.

Where conversation content does go, so teams can judge it:

- **The agent's model provider.** If the agent uses a hosted model, thread text goes to that vendor under the team's own agreement. JotFlow sends only the thread in question and short excerpts of related threads, never channel history.
- **GitHub.** Issue bodies summarise the conversation they came from. If the target repo is public, that summary is public; the card warns when it is.
- **The agent's host.** Some agent CLIs save session transcripts. Presets turn this off where the CLI allows it; where it does not, transcripts stay in the agent container.

There is no hosted component and no telemetry. Nothing is sent to the JotFlow project.

## Permissions

| Component | Needs | Does not need |
|---|---|---|
| JotFlow | A Slack app installed in the team's workspace: `channels:history`, `groups:history`, `chat:write`, `reactions:read`, `reactions:write`, `users:read`, `commands`, and from M5 `app_mentions:read`, plus an app-level token for Socket Mode | Any GitHub access |
| Team's agent | Two GitHub tokens: a read-only token for drafting runs and a write token for execution runs, both scoped to the channel's mapped repos | Any Slack access |

Each team creates a dedicated Slack app for JotFlow from the manifest in this repository, inside their own workspace. The bot's display name and icon are theirs to choose; "JotFlow" is only the manifest default. The app stays internal to that workspace and is never distributed. Reusing an existing app is not supported: scopes, event subscriptions, and slash commands would have to be merged by hand and would collide with whatever the app already does. Slack requires two manual steps that no software can skip, creating the app and clicking Allow on the install screen, so `jotflow init` wraps everything around them: it renders the manifest, opens the create-from-manifest link, validates the pasted tokens, and writes the environment file.

JotFlow must be a Slack app because something has to be woken when a message is posted. Slack's MCP server is pull-only: an agent that is already running can read, search, or post through it, but nothing in it fires on a new message. Only an installed app can subscribe to events. The agent needs no Slack access because JotFlow fetches thread text, hands it over through `get_work`, and does all posting itself.

## Configuration

Two tiers, because every team supplies every value and the two kinds change at different speeds.

| Tier | Lives in | Changed by | Holds |
|---|---|---|---|
| Policy | `jotflow.toml`, deployed with the service | An admin editing the file | Each channel's repos with nickname and description, label allowlists, optional approver list, reaction approval, `issue_links`, debounce window, agent preset |
| Runtime | The local database | Slack slash commands | Channel mode (`propose`, `auto`, `paused`), Slack to GitHub identity links |

Setup is a first-class feature: `jotflow init` asks a few questions and writes `jotflow.toml` plus a `.env` template, and the Slack app manifest installs in a couple of clicks. A team should go from clone to connected bot without reading source.

## Identity

Assignment requires a Slack user to GitHub handle mapping. A self-service slash command lets each person link their own account. Links are self-declared in v1, and cards mark them as unverified.

## Security posture

- Slack content is untrusted input. It reaches only drafting runs. The shipped presets configure drafting runs with read-only GitHub access and no shell or write tools, and execution runs, which can write, receive only the approved structured fields. This holds only when the team gives the agent separate read and write GitHub tokens, and only for the shipped presets; a custom command template is the team's responsibility.
- The agent runs in its own container with an environment allowlist and no access to JotFlow's secrets.
- Proposals are validated against the configured repos and label allowlists before they can be approved, and a reported issue URL must point at the approved repo.
- `ask_channel` posts only in the source thread, strips broadcast mentions such as `@channel`, and is rate-limited.
- MCP tokens are scoped to their run kind and channel; the server validates `Host` and `Origin`.
- Issues and comments are created by the team's agent under whatever identity its GitHub credentials carry, with the proposing Slack user named in the body.
- No secrets in the repository. Configuration is by environment variables. JotFlow's code never uses GitHub credentials.
- Continuous integration runs on forked pull requests with a read-only token. The `pull_request_target` trigger is never used to check out fork code.

## Deferred

- Two-way sync from GitHub comments back into Slack threads.
- Per-action approval granularity.
- Other chat platforms. The orchestrator's Slack dependency should stay behind an interface so this is possible later.

## Stack

Python 3.12 or newer.

| Concern | Choice | Why |
|---|---|---|
| Slack | slack-bolt (AsyncApp, Socket Mode) | Maintained by Slack. Socket Mode means no public URL for self-hosted installs. |
| MCP | the official `mcp` package, FastMCP server over Streamable HTTP | Tool schemas derive from the same Pydantic models used everywhere else. |
| GitHub | none | JotFlow's code never uses GitHub credentials. The team's agent performs reads and writes with its own tooling. |
| State | SQLite via aiosqlite | Pointers and run records. Postgres stays possible behind the same interface. |
| Tooling | uv, ruff, mypy strict, pytest | Locked dependencies, one formatter, typed contracts. |

The Pydantic models in `src/jotflow/models.py` are the single source of truth for the MCP tool contract, the validation applied before a card is posted, and the shape rendered into Slack approval cards.

Package layout:

```
src/jotflow/
  models.py       shared contracts: proposals, runs, classifications, topics, channel modes
  config.py       environment-driven settings, no secrets in the repo
  cli.py          entry point
  orchestrator/   event ingest, keywords, debouncing, topic index, run lifecycle, approval cards
  whiteboard/     the MCP server and token scoping
  adapters/       agent runners and presets
  execution/      execution runs and result checks
```

The major agent CLIs install through npm, so the agent container carries Node. The JotFlow container does not need it.
