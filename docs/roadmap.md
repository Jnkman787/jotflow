# JotFlow roadmap

This is the working plan. [architecture.md](architecture.md) says what JotFlow is; this document says in what order it gets built, what "done" means for each step, and which decisions each step rests on. Each milestone below maps to a GitHub milestone, and each bullet under **Work** maps to an issue.

Status legend: `todo` · `in progress` · `done`

## Decisions

Settled questions that shape the implementation. Change one by editing it here in the same PR as the code that depends on the change.

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | How does the agent hand work back to JotFlow? | **Only through JotFlow's MCP tools.** The agent calls `propose_issue`, `propose_update`, `link_message`, `ask_channel`; JotFlow records each call as it happens. The agent's stdout is ignored. | Printed output differs per agent CLI and would need a fragile parser each. Tool calls are identical everywhere, keep content out of shell arguments, and let the prompt stay tiny: "call `get_work(run_id)`, then act." |
| D2 | Where does configuration live? | **Two tiers.** Policy an admin owns (each channel's repos per D11, optional label lists, optional approver list per D7) lives in a TOML file the team deploys. Day-to-day state (channel mode, identity links) lives in the local database and is changed with Slack slash commands. Every team configures all of this for themselves, so setup must be fast: `jotflow init` asks a few questions and writes the file; the Slack app manifest installs in a couple of clicks. | Mixing tiers means either the file churns or the database silently holds policy. Teams differ in every one of these values, so nothing is baked in. |
| D3 | Does v1 place issues on GitHub Project boards? | **No.** v1 handles repo, title, body, labels, assignees, and size as a label. Proposal cards may show an intended project so the data model is ready. Placement on Projects v2 is a post-v1 feature. | Projects v2 needs organization-level app permission and the GraphQL API; nothing else does. |
| D4 | How is JotFlow tested without a shared Slack workspace? | **Replay plus a live sandbox.** A `jotflow replay <thread.json>` command runs a saved thread through the full pipeline with a scripted fake agent; it is what tests and CI use, and it doubles as a demo. Live experiments happen in a private Slack channel in the maintainer's workspace, writing to a private repository outside this project, never to this one. No workspace, channel, or company detail from the sandbox ever enters this repository. | Contributors and CI cannot depend on someone's Slack workspace. Live testing still matters for Block Kit and Socket Mode behaviour that replay cannot cover. |
| D5 | Who writes to GitHub? | **The team's agent, with the access the team already gave it.** JotFlow's code never uses GitHub credentials; the agent's token lives only in the agent container (D6). After approval, or immediately in `auto` mode, JotFlow launches an execution run; the agent performs the write and calls `record_result` with the issue URL. JotFlow must still be a Slack app, because only an installed app can subscribe to channel events; the agent needs no Slack access. | Removes a whole setup step (no GitHub App), keeps attribution with the team's agent, and matches bring-your-own-agent. Tradeoff: the gate is a flow rule, not a credential boundary, so teams scope their agent's GitHub token to the intended repositories. |
| D6 | How is the agent kept away from JotFlow's secrets? | **Separate containers from v1.** JotFlow and the agent run in two containers started together by one Compose file. The agent container receives an explicit allowlist of environment variables and never JotFlow's `.env` or Slack tokens. Drafting runs get read-only GitHub access and no shell or file-write tools; only execution runs get GitHub write access. Agent presets disable session transcripts where the CLI supports it. | Channel text is untrusted. In one shared container, a prompt-injected agent could read the Slack tokens and every channel the bot can see. Splitting later would force every existing install to change its setup. |
| D7 | What does approval guarantee, and who may approve? | **Integrity always; an approver list is optional and off by default.** Always: the card shows the full issue body; approval locks that exact content by hash; execution runs receive only the approved structured fields, never the thread itself, and the execution prompt treats the approved body as content, never as instructions; the reported issue URL must point at the approved repo; every card records who approved; guests and external Slack Connect users can never approve; a public target repo shows a warning. With approvers off: anyone in the channel may approve, including the poster, and anyone may change the channel mode. With approvers on: only listed approvers may approve or change mode, and no one approves their own proposal. Every mode change is announced in the channel. Reaction approval is off by default. | Teams such as the maintainer's want every member treated equally; others want gatekeepers. Integrity protects both: what gets written is exactly what someone saw and approved. Tradeoff of the open default: it catches agent mistakes, not a member acting alone, so channels with wide or external membership should turn approvers on; JotFlow warns when `auto` is set in a channel with guests or external members. |
| D8 | How are agent runs tracked? | **Persisted run records in the team's own database.** Each run has a kind (drafting, execution, and later question), status, deadline, and linked proposal. At most one run per thread at a time; a newer run supersedes an older pending proposal. On restart, interrupted drafting runs are re-queued and interrupted execution runs are reconciled. Every proposal carries a random hidden marker that the agent looks for by listing recent issues or comments, not by search, before writing, so a retry never duplicates one. JotFlow ignores its own messages and drops events Slack delivers twice. A cheaper model may be configured for execution runs. | Without a run record, restarts orphan work, retries create duplicate issues, and the bot's own replies would wake it up again. |
| D9 | How do conversations map to topics? | **Default rules, no syntax required.** A loose top-level message starts a candidate topic, and the agent may attach it to an existing one. Before proposing a new issue the agent looks for an existing one of any age, open or closed, and prefers updating it. A reply in a thread whose topic has expired revives that topic. Request Changes opens a dialog whose text is posted as a thread reply, so the next run treats it like any other message. The work packet includes short excerpts of likely-related threads so the agent can correlate on content, not titles alone. | The non-engineer's whole experience rests on these defaults; they had to be settled before the agent loop is built. |
| D10 | How can people steer JotFlow explicitly? | **Optional keywords at the start of a message, always followed by a colon, parsed by JotFlow itself.** v1: `idea:` and `bug:` are hints to the agent, which may overrule them on the card; `re:` attaches the message to an existing topic or issue in this channel's repos; `skip:` marks it as not an idea. `ask:` arrives with D12 in M5. `hold:`, `go:`, and `new:` are post-v1. Each keyword has an equivalent reaction. JotFlow strips the keyword before the agent sees the text, and no keyword can skip approval or change the mode. With approvers off, anyone may skip someone else's message, with a visible note in the thread and an undo for the author; with approvers on, only the author or an approver. Typos are tolerated only on keywords of four or more letters. `/jotflow help` and a pinned help message teach the set. | No @-mention is required, most people never need a keyword, and one rule (word plus colon) is easy to learn. The colon stops sentences like "Skip button is broken" from being read as commands. |
| D11 | What if a channel feeds several repos? | **Each channel lists its repos with a nickname and a one-line description; the agent picks one.** The card shows the chosen repo prominently with a dropdown to change it before approval. When unsure, the agent asks in the thread. `re:` accepts a bare issue number when unambiguous and `re: web#42` otherwise; an ambiguous bare number prompts a question. A channel with one repo needs none of this. | Teams often discuss several codebases in one place. A wrong repo guess should cost one click, not a rejected proposal. |
| D12 | Can people ask JotFlow questions? | **Yes, in M5, read-only.** `ask:` or an @-mention of the bot starts a question run that answers in the thread using only this channel's topics and mapped repos. Separately, an admin can issue a read-only, revocable operator token limited to `list_topics` and `get_topic` on named channels, for querying from a terminal. Neither can propose, post outside the thread, or record results. | Asking the bot about an issue was part of the original vision and had dropped out of the plan. Read-only scope keeps it from widening the attack surface. |
| D13 | How do agents authenticate to JotFlow's MCP server? | **Opaque per-run bearer tokens minted by JotFlow, not OAuth.** Authorization is optional in the MCP specification and there is no third-party authorization server, so OAuth 2.1 and protected-resource metadata are not used. The server still meets the specification's applicable requirements: `Authorization` header on every request, never a token in a URL, 401 on an invalid token, no token passthrough, and 403 on any request with an `Origin` header. Revisit for the post-v1 cross-machine adapter, which also requires TLS. | Reviewed against the 2026-07-28 MCP specification and its security best practices. OAuth would add an authorization server for a token whose only audience is JotFlow itself. |

Smaller decisions deferred until their milestone: expiring a topic when its linked issue closes (#37; the activity window is settled as `topic_window_days`, default 14, in #12), and identity verification versus self-declaration (v1 self-declares and marks unverified identities on the card).

## Milestone 1: Installable `todo`

**Goal.** A team can deploy JotFlow and see it connect to Slack, even though it does nothing yet.

**Work**

- Slack app manifest with the scopes and events JotFlow needs, including reaction events, Socket Mode enabled; the app stays internal to the workspace, never distributed
- Config loading: `Settings` from environment, `jotflow.toml` schema for each channel's repos with nickname and description (D11), optional label lists (any existing repo label by default, #92), optional approver list off by default and reaction approval off by default (D7), `issue_links`; drop the GitHub App fields and rename the `github` package to `execution` (D5)
- `jotflow init`: guided setup. Writes `jotflow.toml`, renders the Slack manifest with the team's chosen bot name, prints a create-from-manifest deep link, waits for the two tokens, validates them against Slack's auth endpoint with scope-specific error messages, writes `.env`, confirms the connection, and records the chosen agent preset (the agent smoke test lands with the presets in M3)
- `jotflow run`: connects over Socket Mode, joins configured channels, logs redacted events, ignores the bot's own messages, and drops duplicate event deliveries (D8)
- Dockerfile and `docker-compose.yml` for the JotFlow container with a persistent volume for the database; the agent container arrives in M3 (D6)
- Quickstart in the README: clone to connected bot
- Record design review decisions D6 to D12 (this change)

**Done when** a fresh clone with valid credentials runs `jotflow run` and logs a redacted event for a message posted in a configured channel.

## Milestone 2: Whiteboard `todo`

**Goal.** JotFlow holds the topic index and exposes it over MCP. Nothing calls an agent yet.

**Work**

- SQLite store: message pointers, topics, proposals, identity links, channel modes (see data model in architecture.md)
- Run records: kind, status, deadline, linked proposal; one run per thread; supersede; restart recovery (D8)
- Topic index: active topics per channel within a rolling window
- MCP server over Streamable HTTP mounted in the orchestrator process
- Tools: `list_topics`, `get_topic`, `get_work`, `propose_issue`, `propose_update`, `link_message`, `ask_channel`, `record_result`
- Per-run bearer token scoped to the run's kind and channel, with Host and Origin validation on the server (D6)
- `jotflow replay <thread.json>`: feed a saved thread through ingest and the store with a scripted fake agent
- Fixture threads for tests and demo

**Done when** `jotflow replay` on a fixture produces a topic in the store, and a terminal agent pointed at the MCP server can list it.

## Milestone 3: Agent loop `todo`

**Goal.** A real agent reads a thread and produces proposals through the tools.

**Work**

- Debouncer: per-thread quiet window before a run; loose top-level messages start candidate topics; replies revive expired topics (D9)
- Intent keywords `idea:`, `bug:`, `re:`, `skip:` and their reactions, parsed by JotFlow (D10)
- Work packet: thread text fetched live from Slack plus the compact topic index and excerpts of likely-related threads, several topics per thread allowed, served by `get_work` (D9)
- Generic command adapter: command template, MCP config injection, per-run token, timeout, environment allowlist
- Agent container: separate from JotFlow, started by the same Compose file, with a runner on the internal network that starts runs; no Docker socket, no published ports, read and write GitHub tokens chosen per run kind (D6)
- Presets for Claude Code and Codex CLI: read-only GitHub and no shell or write tools for drafting runs, transcripts off (D6)
- Minimal end-to-end execution with one preset against the sandbox, to prove D5 before the gate is built
- Agent prompt: classify messages, honour keyword hints, choose among the channel's repos, search duplicates with read-only GitHub access, propose, ask when unsure (D10, D11)
- Proposal capture: tool calls recorded with `pending` state
- Logging that redacts message bodies by default

**Done when** a fixture thread replayed with a real agent yields a pending `IssueProposal` with a plausible title and body, and chatter is classified as chatter.

## Milestone 4: Gate `todo`

**Goal.** Proposals become GitHub issues under human control.

**Work**

- Block Kit proposal card: full body, repo with a dropdown to change it, labels, assignees, size, confidence, public-repo warning (D7, D11)
- Approve and Request Changes actions; approval locks the content by hash and records the approver; Request Changes opens a dialog whose text becomes a thread reply (D7, D9)
- Channel modes `propose`, `auto`, `paused` via `/jotflow mode`; who may change them follows D7; every change is announced
- Proposal validation against the repo, and the repo's label list where one is set, before a card is posted
- Execution runs: after approval, or immediately in `auto`, launch the agent with only the approved fields; it checks for the proposal marker, writes with its own GitHub access, and calls `record_result`; the URL must match the approved repo (D5, D7, D8)
- Execution prompt and presets: how each supported agent CLI is expected to reach GitHub (GitHub MCP server or `gh`)
- Every created issue carries its Slack permalinks; every linked Slack message gets a reply with the issue URL
- Approver policy: optional approver list, off by default; no self-approval when on; guests and external users never approve (D7)

**Done when** a message in a `propose` channel yields a card, Approve triggers an execution run in which the agent creates the issue in the sandbox repo, the thread receives the issue link, and a follow-up reply yields an update proposal.

## Milestone 5: Polish `todo`

**Goal.** Ready for another team to adopt.

**Work**

- `/jotflow link <github-login>`: self-service identity linking; cards mark unverified identities
- Questions: `ask:` and @-mentions answered read-only in the thread (D12)
- Operator token: admin-issued, read-only, revocable, for terminal queries (D12)
- Presets for Gemini CLI and Copilot CLI
- CI hardening: pin Actions to commit SHAs, add a workflow linter
- Reuse before creating: the agent searches open and closed issues of any age across the channel's repos, plus JotFlow's full topic history, and proposes an update or asks instead of creating a duplicate; the best candidate is surfaced on the card with a one-click "same as" action
- Topic expiry: quiet window or linked issue closed; expiry removes a topic from the active index only, and its record is kept indefinitely for matching
- Rebuild command: reconstruct the store from Slack thread replies
- Container image published to GHCR on tagged releases
- Python package published to PyPI with trusted publishing, from the same tag and version as the image; `jotflow init` writes a Compose file pinned to that image, so a team installs with `uvx jotflow init` then `docker compose up -d` and never clones the repo
- Documentation: deployment guide, agent adapter guide, threat model
- First tagged release

**Done when** a fresh-eyes review (a new agent with no project context reading only the README and setup prompts as a non-engineer) passes under the three-pass cap in AGENTS.md, with no BLOCKER and no contradiction left and every deferred minor finding in #114 fixed or declined, and the maintainer, unguided, completes the no-clone install from an empty folder and files an issue through Slack. JotFlow has a single maintainer until v1, so outside testers are not a gate; feedback from the first real adopters is the first post-v1 task.

## Post-v1

- Collect and act on feedback from the first outside adopters: a fresh install by someone who has never seen JotFlow

- Projects v2 board placement (D3)
- Keywords `hold:`, `go:`, and `new:` (D10)
- Two-way sync: GitHub comments back into Slack threads
- Per-action approval granularity
- HTTP and SDK agent adapters
- Other chat platforms behind the Slack interface
- Slack manifest API support in `jotflow init`: create the app and push scope updates programmatically for teams that supply an app configuration token
