# Upshot roadmap

This is the working plan. [architecture.md](architecture.md) says what Upshot is; this document says in what order it gets built, what "done" means for each step, and which decisions each step rests on. Each milestone below maps to a GitHub milestone, and each bullet under **Work** maps to an issue.

Status legend: `todo` · `in progress` · `done`

## Decisions

Settled questions that shape the implementation. Change one by editing it here in the same PR as the code that depends on the change.

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | How does the agent hand work back to Upshot? | **Only through Upshot's MCP tools.** The agent calls `propose_issue`, `propose_update`, `link_message`, `ask_channel`; Upshot records each call as it happens. The agent's stdout is ignored. | Printed output differs per agent CLI and would need a fragile parser each. Tool calls are identical everywhere, keep content out of shell arguments, and let the prompt stay tiny: "call `get_work(run_id)`, then act." |
| D2 | Where does configuration live? | **Two tiers.** Policy an admin owns (channel to repo mapping, repo and label allowlists, approver list) lives in a TOML file the team deploys. Day-to-day state (channel mode, identity links) lives in the local database and is changed with Slack slash commands. Every team configures all of this for themselves, so setup must be fast: `upshot init` asks a few questions and writes the file; Slack and GitHub App manifests install in a couple of clicks. | Mixing tiers means either the file churns or the database silently holds policy. Teams differ in every one of these values, so nothing is baked in. |
| D3 | Does v1 place issues on GitHub Project boards? | **No.** v1 handles repo, title, body, labels, assignees, and size as a label. Proposal cards may show an intended project so the data model is ready. Placement on Projects v2 is a post-v1 feature. | Projects v2 needs organization-level app permission and the GraphQL API; nothing else does. |
| D4 | How is Upshot tested without a shared Slack workspace? | **Replay plus a live sandbox.** A `upshot replay <thread.json>` command runs a saved thread through the full pipeline with a fake GitHub writer; it is what tests and CI use, and it doubles as a demo. Live experiments happen in a private Slack channel in the maintainer's workspace, writing to a throwaway sandbox repository, never to this one. | Contributors and CI cannot depend on someone's Slack workspace. Live testing still matters for Block Kit and Socket Mode behaviour that replay cannot cover. |

Smaller decisions deferred until their milestone: topic expiry, default approver policy and reaction-as-approval, what Request Changes does, identity verification versus self-declaration, debounce strategy for loose messages versus thread replies.

## Milestone 1: Installable `todo`

**Goal.** A team can deploy Upshot and see it connect to Slack, even though it does nothing yet.

**Work**

- Slack app manifest with the scopes and events Upshot needs, Socket Mode enabled
- GitHub App manifest with issues write and metadata read permissions
- Config loading: `Settings` from environment, `upshot.toml` schema for channel to repo mapping, repo and label allowlists, approver list
- `upshot init`: interactive setup that writes `upshot.toml` and a `.env` template
- `upshot run`: connects over Socket Mode, joins configured channels, logs redacted events
- Dockerfile and `docker-compose.yml` with a persistent volume for the database
- Quickstart in the README: clone to connected bot

**Done when** a fresh clone with valid credentials runs `upshot run` and logs a redacted event for a message posted in a configured channel.

## Milestone 2: Whiteboard `todo`

**Goal.** Upshot holds the topic index and exposes it over MCP. Nothing calls an agent yet.

**Work**

- SQLite store: message pointers, topics, proposals, identity links, channel modes (see data model in architecture.md)
- Topic index: active topics per channel within a rolling window
- MCP server over Streamable HTTP mounted in the orchestrator process
- Tools: `list_topics`, `get_topic`, `search_issues`, `propose_issue`, `propose_update`, `link_message`, `ask_channel`, `get_work`
- Per-run bearer token so only the agent run Upshot launched can call the server
- `upshot replay <thread.json>`: feed a saved thread through ingest and the store with a fake GitHub writer
- Fixture threads for tests and demo

**Done when** `upshot replay` on a fixture produces a topic in the store, and a terminal agent pointed at the MCP server can list it.

## Milestone 3: Agent loop `todo`

**Goal.** A real agent reads a thread and produces proposals through the tools.

**Work**

- Debouncer: per-thread quiet window before a run; strategy for loose top-level messages
- Work packet: thread text fetched live from Slack plus the compact topic index, served by `get_work`
- Generic command adapter: command template, MCP config injection, per-run token, timeout
- Presets for Claude Code, Codex CLI, Gemini CLI, Copilot CLI
- Agent prompt: classify messages, search duplicates, propose, ask when unsure
- Proposal capture: tool calls recorded with `pending` state
- Logging that redacts message bodies by default

**Done when** a fixture thread replayed with a real agent yields a pending `IssueProposal` with a plausible title and body, and chatter is classified as chatter.

## Milestone 4: Gate `todo`

**Goal.** Proposals become GitHub issues under human control.

**Work**

- Block Kit proposal card: repo, title, body summary, labels, assignees, size, confidence
- Approve and Request Changes actions; Request Changes opens a modal whose text becomes a thread reply
- Channel modes `propose`, `auto`, `paused` via `/upshot mode`
- GitHub writer through the GitHub App: create issue, edit body, comment, labels, assignees, enforced allowlists
- Every created issue carries its Slack permalinks; every linked Slack message gets a reply with the issue URL
- Human override: reaction or reply re-links a message to another topic
- Approver policy: anyone in channel by default, optional approver list

**Done when** a message in a `propose` channel yields a card, Approve creates the issue in the sandbox repo, the thread receives the issue link, and a follow-up reply yields an update proposal.

## Milestone 5: Polish `todo`

**Goal.** Ready for another team to adopt.

**Work**

- `/upshot link <github-login>`: self-service identity linking
- Duplicate detection before proposing, surfaced on the card
- Topic expiry: quiet window or linked issue closed
- Rebuild command: reconstruct the store from Slack and GitHub
- Container image published to GHCR on tagged releases
- Documentation: deployment guide, agent adapter guide, threat model
- First tagged release

**Done when** someone outside the maintainer's team installs from the README alone and files an issue through Slack.

## Post-v1

- Projects v2 board placement (D3)
- Two-way sync: GitHub comments back into Slack threads
- Per-action approval granularity
- HTTP and SDK agent adapters
- Other chat platforms behind the Slack interface
