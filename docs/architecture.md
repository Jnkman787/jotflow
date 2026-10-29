# JotFlow architecture

This document is the design contract the implementation is built against. Change it through a pull request when a decision changes. The build order, milestone definitions, and the numbered decisions D1 to D13 are in [roadmap.md](roadmap.md).

## Problem

A small team wants anyone, including people without a coding agent or GitHub fluency, to propose work by writing in a shared Slack channel. An AI agent should turn those conversations into well-formed GitHub issues, keep the issues current as the conversation continues, and keep the whole team informed without a separate "I filed this" message. The team supplies its own agent. The tool must be usable by any team, with any agent, on their own infrastructure.

## Shape

JotFlow is an **application with an MCP surface**, not an MCP server on its own and not a framework-specific agent.

An MCP server cannot initiate anything; it answers requests from an agent that is already running. JotFlow is event-driven: Slack produces events, something must be listening, hold state across days, debounce bursts, and decide when to invoke an agent. That something is the orchestrator. Tying the project to one agent framework would contradict the goal of working with any agent, so the orchestrator is framework-free and exposes its capabilities over MCP, which every major agent CLI and SDK already speaks.

### Components

**Orchestrator.** A long-running service.

- Subscribes to Slack events for configured channels over Socket Mode. Ignores its own messages and drops events Slack delivers twice.
- Is invite-only: at startup it checks that it is a member of each configured channel and names any it is not in, with the `/invite` instruction, rather than joining. Message edits and deletes are ignored in v1, since editing a message never changes what is approved.
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
| `get_work` | The run's packet: thread text, the active topic index, and related-thread excerpts for a drafting or question run; only the approved structured proposal for an execution run |
| `get_issue` | Question runs only: one issue in one of the run's channel repos, by number. State, close reason, title and labels marked untrusted, comment count and last update; never the body or comments. Read through the drafting runner, at most 5 a run (#273) |
| `propose_issue` | Structured draft: repo, title, body, labels, assignees, size |
| `propose_update` | Structured change to an existing issue: body edit, comment, label or assignee change |
| `link_message` | Associate a Slack message with a topic, or mark it as chatter |
| `ask_channel` | Post a clarifying question into the source thread only |
| `list_repo_docs`, `read_repo_doc` | Drafting only, and only when a repo sets `read_paths`: the repo's design docs that pattern allows, by path, and one doc's text, marked untrusted (#209) |
| `record_result` | Report the issue URL, or the error, after executing an approved proposal |

Every tool reads its channel, thread, run, and proposal from the caller's token, never from its arguments. A topic in another channel is reported exactly like a missing one. Text written in Slack, and anything an earlier run wrote, such as topic titles, sits in fields marked untrusted, and each packet carries a fixed notice saying how to treat them. No such text reaches a tool description. A failure the agent should act on, such as a finished run or an unknown topic, comes back as a tool result with `isError` set and a short reason. Tools also advertise an output schema, so those labels show in what the agent reads.

Every call carries a token, and what a token may do depends on who holds it:

| Token | Issued to | May call |
|---|---|---|
| Drafting run | One drafting run, one channel | `get_work`, `list_topics`, `get_topic`, `propose_*`, `link_message`, `ask_channel`, and `list_repo_docs` and `read_repo_doc` when a repo sets `read_paths` |
| Execution run | One execution run, one approved proposal | `get_work`, `record_result` |
| Question run (D12, #57) | One question run, one channel | `get_work`, `list_topics`, `get_topic`, `get_issue` (this channel's repos only, #273), `answer` (once, in the source thread only) |
| Operator token (M5, D12, #58) | One person, named channels, admin-issued and revocable | `list_topics`, `get_topic` only: no `get_work`, no write, approval or result tool |

Tokens are short-lived for runs and revocable for operators. The server validates `Host` and `Origin` headers so a web page cannot reach it from a browser.

**Run tokens.** The run lifecycle issues one token per run from an in-memory registry and revokes it when the run ends; a time limit, at most two hours, ends it otherwise. A token is `jfr_` followed by 32 random bytes, so secret scanners can recognise a leaked one. The registry keeps only its SHA-256 digest, looks tokens up by that digest so timing reveals nothing about stored tokens, does no I/O, and never logs or prints it. Tokens are not persisted: a restart ends every run, and re-queued runs get fresh tokens. A token's grant names its run, run kind, and channel, plus the thread for a drafting or question run and the one approved proposal for an execution run. A run for a top-level post uses the post's own timestamp as its thread, as Slack does for a thread's parent, so a drafting or question grant always names one thread and tools compare it exactly. Each tool declares the run kinds that may call it, matching the table above. A run lists only its kind's tools, and a call to another kind's tool gets the same error as a tool that does not exist.

**Operator tokens.** An admin issues one with `jotflow operator create --name … --channel …`, for 30 days by default and 365 at most. It is `jfo_` followed by 32 random bytes, printed once; the database keeps only its SHA-256 digest, with its name, channels, expiry, revocation and last use (#58). The running server checks a copy in memory, so the gate still does no I/O. The copy is reloaded every 2 seconds, so `jotflow operator revoke` takes effect within that interval. A token the copy doesn't know triggers one reload, at most once a second, so a new token works at once and guessed tokens can't load the database. While guesses are arriving, a token created a moment ago can wait for the next 2-second reload. The grant names the token's channels. `list_topics` takes an optional `channel`, which an operator with more than one channel must give, and `get_topic` reads any of them. Each operator call is logged to `jotflow.operator.audit` with the token id, tool, channel and time, never topic content. No port is published for it: a terminal MCP client runs `docker compose exec -T -e JOTFLOW_OPERATOR_TOKEN jotflow jotflow operator connect`, on the host or over ssh. That stdio bridge forwards `tools/list` and `tools/call` to the server on loopback inside the container, and has no tools of its own.

**Write tools.** The five write tools are the agent's only way to act, and each one is bounded by the run's grant:

| Tool | Rules |
|---|---|
| `propose_issue` | Needs a topic in the run's channel with no issue yet. Uses only the channel's repos and allowed labels. Sources must come from the run's packet. An assignee must be the linked GitHub login of a participant. Participants are recorded but never become assignees. An `@login` in the title or body must be a participant's linked GitHub login too, outside code (#250). The new proposal supersedes the thread's older pending ones. At most 3 proposals per run. |
| `propose_update` | Only for the issue the topic is already linked to, with the same repo, label, source, assignee, and `@login` rules |
| `link_message` | Only messages the run's `get_work` packet served. A person's earlier decision always stands. At most 200 per run. |
| `ask_channel` | Posts only into the run's own thread, with broadcast mentions removed and Slack markup escaped. Never unfurls or turns names into mentions. At most 2 per run, and one per thread every 30 seconds across runs. |
| `record_result` | Execution runs only, for the grant's own proposal while it is executing, or reconciling after a restart. An issue URL must match the approved repo and issue. A result is recorded once. |

**Repo docs (#209).** A repo in `jotflow.toml` may set `read_paths`, such as `["README.md", "docs/plans/**/*.md"]`. Only then does JotFlow serve `list_repo_docs` and `read_repo_doc`, to drafting runs only. With no repo setting it, the tool set is exactly as before. JotFlow holds no GitHub token, so the drafting runner fetches, with the read token, over `POST /docs/list` and `POST /docs/read`, behind its secret and gate. The checks are JotFlow's, before the runner is asked:

| Check | Rule |
|---|---|
| Repo | One of the run's channel's listed repos that sets `read_paths`. A repo found by a scope never gets doc reads. |
| Pattern | Relative POSIX paths with `*`, `?` and `**`. A wildcard never matches a hidden segment, `.git`, `.github/workflows` and `.env*` are refused even written out, and only `.md`, `.markdown`, `.txt`, `.rst` and `.adoc` files are readable. Checked when the policy loads. |
| Path | Exactly a path the default branch's tree listed, as a regular file. A symlink or submodule is never listed. The runner reads the blob by the tree's sha, so nothing is followed. |
| Caps | 64 KiB a file, cut with a marker. 20 reads and 256 KiB a run, and 500 listed paths. Content must be strict UTF-8 with no NUL. |

Doc text comes back as untrusted data. Each read goes on the run's ledger as repo, path and blob sha, never content, and the card shows a "Read:" line listing them. JotFlow keeps sampled hashes of what each read returned while the run lasts. A proposal that copies a passage of about 200 characters word for word gets a "Contains a long passage copied word for word" line on its card. Execution packets never include docs. At startup JotFlow asks the drafting runner to list each such repo, and warns if it can't or nothing matches.

Every call also checks that its run is still running, as a second guard behind token revocation. It does not check the deadline: the token's short grace after the deadline lets a run's last call land, such as the result of a write that already happened, while the overdue sweep times the run out. `get_work` does refuse a run past its deadline, so no new work starts late.

Every call is idempotent. A repeat with the same arguments returns the first result and writes nothing, so a retry after the request timeout cannot duplicate a proposal or a question. The write and its ledger entry run in a cancellation-shielded scope, so the timeout cannot cut them apart. The ledger holds ids and states, never message text. A refusal, such as a cap or a topic outside the channel, comes back as a tool result marked as an error, with a reason written in code.

**Transport rules.** `jotflow run` serves the endpoint at `/mcp` on `JOTFLOW_MCP_HOST:JOTFLOW_MCP_PORT`, and a gate in front of the MCP SDK applies these rules to every request before the SDK sees it:

| Request | Answer |
|---|---|
| Carries an `Origin` header | 403. Agents never send one, so this is stricter than an allowlist. |
| `Host` not loopback on the configured port, and not in `JOTFLOW_MCP_ALLOWED_HOSTS` | 403, against DNS rebinding |
| Any method but POST | 405 with `Allow: POST`. There are no sessions, so there is no GET stream and no DELETE. |
| A query string | 400, so a token never travels in a URL |
| `MCP-Protocol-Version` missing or other than `2026-07-28` | 400 with the unsupported-version error listing what is served. The handshake era is not served, so no `Mcp-Session-Id` is ever minted. |
| A body over 64 KiB | 413 |
| No valid `Authorization: Bearer` token | 401 |
| Headers that disagree with the body | 400 `HeaderMismatch`, from the SDK |
| Not finished within 30 seconds | 504 |

The server declares only the `tools` capability, with `listChanged: false`: no prompts, resources, subscriptions, logging, completions, sampling, or elicitation. Tools come from a fixed registry in code, so no runtime content can add a tool or change a description. Every request, admitted or not, writes one audit line with the method, tool, run, status, outcome, and duration, never arguments, bodies, or tokens. Until per-run tokens land (#16), the token check refuses every token, so nothing is reachable.

**Agent adapters.** Thin runners that launch the team's agent with the MCP server attached and a prompt.

- First adapter: a generic command template. Every major agent CLI has a headless mode and reads an MCP configuration, so a team selects their agent by editing one line. Presets ship for Claude Code and Codex CLI first, then Gemini CLI and Copilot CLI.
- A preset defines, per run kind, which of the CLI's tools are allowed. Drafting runs get no shell and no file-write tools, and only the team's read-only GitHub token. Execution runs get the write token, used to create or update one issue. Presets disable session transcripts where the CLI supports it.
- Later: an HTTP adapter and a reference SDK adapter.

**Agent output contract.** The agent acts only by calling the whiteboard MCP tools. JotFlow records each call as it happens; the agent's stdout is ignored. The prompt handed to the agent is minimal: the instruction to call `get_work` and then act. `get_work` takes no arguments, because the run's token already names the run, its channel, and its thread. The drafting instructions live in JotFlow, not in the preset (#23). They are `src/jotflow/whiteboard/instructions/drafting.md`, served in the drafting packet's `instructions` field, so every CLI gets the same text, versioned with the tools it describes. They cover how to classify, choose a repo, search for duplicates, propose, treat people, and ask. The packet also carries the facts those rules need: the channel's repos with their descriptions and label lists, from the policy, and the thread's people with their linked GitHub logins. `jotflow replay --agent <preset>` checks a real agent against the saved fixtures, through the real runner and MCP server, with a fixture-backed stand-in for GitHub's read tools. Replay plays question runs too (#270). The live question handler queues them from the fixture's messages, so a guest is refused by the real code. Each answer is checked as posted, after the no-links rewrite, by a link and ping matcher of replay's own.

**Execution runs.** JotFlow's code never uses GitHub credentials. When a proposal is approved, or immediately in `auto` mode, JotFlow launches the agent again. Its work packet contains only the approved structured fields, never the thread itself. The approved title and body were drafted from Slack and can carry injected text verbatim, so they are passed as labelled content to write, and the execution prompt treats them as content, never as instructions. Every proposal carries a random marker id, written into the issue body or comment as a hidden HTML comment; it is random so it reveals nothing even with `issue_links = "none"`. Before writing, the agent lists the target repo's recently created issues, or the issue's recent comments for an update, and looks for the marker. It lists rather than searches because GitHub's search index lags and a quick retry could miss a fresh issue. Then it performs the write with the GitHub access the team gave it and calls `record_result` with the issue URL. JotFlow checks that the URL points at the approved repo, stores it, replies in the Slack thread, and updates the card. The reply and a reaction on each source message (`issue_reaction`, `ticket` by default) go out through the store's outbox once the proposal is applied, never from the agent's write (#32). Reactions go first, since repeating one is harmless, so a retry never posts the reply twice. The reply is built by JotFlow from the repo's nickname and the checked URL, and it carries the URL even with `issue_links = "none"`, so a rebuild can find it. A team may configure a cheaper model for execution runs, since they make a single write.

**Exact writes (#31).** JotFlow, not the agent, decides what is written. The execution packet carries JotFlow's own instructions (`src/jotflow/whiteboard/instructions/execution.md`), the checks to make first, and the exact GitHub MCP tool calls to copy, built by `src/jotflow/execution/writes.py`:

- **A new issue** is one `issue_write` create.
- **An update** is an `issue_write` update for a body, label, or assignee change, then an `add_issue_comment` for a comment. GitHub replaces the whole label and assignee set on update, so those changes are given as additions and removals against the issue's current set, which the agent reads first. A replaced body keeps the footer, with this update's attribution, permalinks, and marker. It also keeps every JotFlow marker line the old body had, such as the creating proposal's, which a retry or a rebuild (#38) looks for. Those come from the issue's current body, not from JotFlow's store, so they survive a rebuilt store (#167).
- **The footer.** Every planned body and comment ends with a footer, then the Slack permalinks and the hidden marker. A new issue's footer says "Raised in Slack by <poster>, with contributions from <names>", and an update's says "Updated from Slack by <names>" (#307). With `issue_links = "none"`, it is the marker alone.

The poster comes from the thread root's author, using the same lookup approval uses, and a bot-posted root names no one. A new issue credits everyone who contributed. That means the Slack-confirmed authors of its cited sources, and of every other message linked to its topic in the same thread, in time order. It never credits a message from another thread, a message marked as chatter, or the agent-written participants list. An update credits who added the new information: the authors of its own sources. Each person is named once, and after nine names the rest are counted, as "and 6 others". Only messages up to the moment the draft was made count, and the card shows the same names, worked out the same way, as "Credited in the issue: raised by Sam, with contributions from Alex". So every name sent to GitHub is one an approver saw (#307 review). A message added later reaches the issue through an update, which credits its author then. If the card can't look the names up in time, it says so. The workspace URL is fetched before the people lookups, so a slow Slack can cost names but never the links. Display names are cleaned so they can't mention, link, or break the markdown. That removes HTML entities such as `&#64;`, bidirectional overrides, and zero-width characters. The permalinks come from each source's own link, or are built from the workspace URL that the startup `auth.test` returns. A lookup that fails leaves its part out and never stops the write.

Before writing, the agent looks for the marker where the checks say, and confirms every approved label exists. If a label is missing, it reports an error naming the labels and writes nothing, because GitHub would otherwise create the label. `jotflow replay --execute --agent <preset>` checks this end to end: it approves each fixture's proposals, runs their execution runs through the real exec runner and MCP server against a fake GitHub with write tools, and compares every write with the planned one exactly. It then repeats the check after a scripted interrupted attempt, so the run under test must find the marker and write nothing.

**Protocol and authorization (D13).** The server implements the current MCP specification's Streamable HTTP transport and pins the protocol versions it serves. It does not use OAuth: JotFlow mints opaque, short-lived bearer tokens for its own runs, so there is no third-party authorization server and no token is ever passed through to another service. It meets the specification's authorization requirements that apply: the token travels in the `Authorization` header on every request, never in a URL; an invalid token gets 401; any request carrying an `Origin` header gets 403, since agents never send one. The tool list is fixed for the lifetime of a run, and the server declares no sampling or elicitation capability.

## Deployment

JotFlow and the agent run in **two containers**, started together by one Compose file (D6). Running it on a server, backups, upgrades, and what another orchestrator must keep are in [deployment.md](deployment.md).

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

**How runs start.** JotFlow never mounts the Docker socket, which would give it root on the host and reach into the agent's container. Instead, the agent container runs a small **runner** that listens on the internal Compose network. To start a run, JotFlow sends the runner a run id, run kind, and per-run token; the runner launches the agent CLI with the matching preset and token, and the agent calls back over MCP. The runner accepts only those three fields, never a command. This is part of v1 and separate from the post-v1 HTTP adapter, which lets the runner live on another machine.

**Network.** The MCP server and the runner listen only on the internal Compose network, with no ports published to the host. The MCP server's `Host` allowlist includes its Compose service name. Outside Compose, the MCP server defaults to `127.0.0.1`.

**GitHub tokens.** The agent container holds two GitHub tokens, a read-only one and a write one. The runner passes the read-only token to drafting and question runs and the write token only to execution runs, so read-only drafting does not rest on CLI tool settings alone. The shipped presets reach GitHub through GitHub's MCP server, which they start in read-only mode for drafting and question runs, as a second layer.

The agent container receives an explicit allowlist of environment variables. It never sees JotFlow's `.env`, Slack tokens, or database. JotFlow's container never holds the agent's GitHub token. A prompt-injected agent therefore cannot reach JotFlow's Slack tokens. It can still misuse its own GitHub access within its run kind's tool allowlist; in particular, read-only access during drafting can copy private repo content into a proposal body or a clarifying question in Slack. Channel repo mappings and the agent's read token should cover only repos whose content may be discussed in that channel.

The team decides where both containers run. The only requirements are that JotFlow can start runs in the agent container and the agent can reach JotFlow's MCP port. A split across machines is the post-v1 HTTP adapter.

**The JotFlow image.** `docker/Dockerfile` builds from `uv.lock` and ships only the virtual environment: no uv, no Node, no agent CLI. It runs as an unprivileged user (uid 10001). `docker-compose.yml` mounts a named volume at `/data` for the database, keeps the root filesystem read-only, drops all capabilities, and publishes no ports. JotFlow joins two networks: `egress` for Slack, and `agents`, which is internal and has no route out. The M3 agent service joins only `agents` plus whatever egress its model and GitHub access need.

**The agent containers.** `docker/Dockerfile.agent` builds one image for two Compose services, from the same pinned Python base plus Debian's Node for the agent CLIs. Debian's npm installs them and is then purged, along with the old node modules it brings. The CLIs install through the `AGENT_NPM_PACKAGES` build argument, which `docker/docker-compose.yml` sets to the versions the presets were verified with. The image also holds GitHub's MCP server, pinned by release and by a sha256 per architecture.

| Service | Runs | Env file | GitHub token | Home volume |
|---|---|---|---|---|
| `agent` | drafting and question | `agent.env` | read-only | `agent-home` |
| `agent-exec` | execution only, one at a time | `agent-exec.env` | write | `agent-exec-home` |

Two services, not one, because runs share a uid. A same-uid process can read another's `/proc/<pid>/environ`, the runner's included, and its run directory, and Linux does not stop that. In one container, a drafting run with nothing more than a file-read tool could therefore read the write token. Split by kind, the write token never exists in a container a drafting run can reach, and a drafting run cannot plant files in the home an execution run loads. Within one service, runs can read each other's environment and run directory by default. That is accepted, because they hold the same token. **Per-run uids (#181)** close it, opt-in: with `JOTFLOW_RUNNER_ISOLATION=uid` in an agent env file and `docker-compose.isolation.yml` in `COMPOSE_FILE`, the runner starts as root with only SETUID and SETGID, and refuses to start otherwise. Each run's agent runs as its own uid from a pool of 64, with no supplementary groups and, by the kernel's rule, no capabilities, which the runner checks right after the start. A run's directory is `0770 root:runN` and its config `0640`, given to a per-run group with `setgroups` rather than chown. `/tmp` is the runner's own 0711 directory, and each run has its own home and scratch space. The runner can't signal another uid, so a helper running as the run's uid stops it with `kill(-1)` and, when it ends, empties its directory and its `/dev/shm` files. A uid that didn't sweep clean isn't reused. A tiny init as PID 1 reaps the orphans. The mode needs an API key, not a CLI login, since each run has its own home.

Both services run as uid 10002 with a read-only root, `/tmp` in memory, all capabilities dropped, and `no-new-privileges`. Neither gets `.env` or any bind mount. Each has its own internal network to JotFlow, `agents` or `agents-exec`, so the drafting container cannot reach `agent-exec` at all, and its own egress for the model API and GitHub. Neither publishes a port. Both sit behind the Compose profile `agent`, so they start only once a team sets `COMPOSE_PROFILES=agent`. `jotflow init` adds it once both env files hold their GitHub tokens. In each, `jotflow runner` listens on port 8766, at `agent:8766` and `agent-exec:8766`, and refuses run kinds its service does not serve. JotFlow's client (#21) routes each run by kind. A runner refuses to start if its env file holds the other kind's token:

- **The request.** JotFlow starts a run with `POST /runs`, carrying exactly the run id, run kind, and run token. It follows the run with `GET /runs/{id}` and stops it with `DELETE /runs/{id}`. Every call carries that runner's own secret, compared in constant time and never given to the agent. The two runners have different secrets: `agent.env`'s matches `JOTFLOW_RUNNER_SECRET` in `.env`, and `agent-exec.env`'s matches `JOTFLOW_RUNNER_EXEC_SECRET`. A drafting run that reads its own runner's secret from `/proc` therefore still cannot start anything on `agent-exec`. The runner refuses an `Origin` header, a `Host` off its allowlist, a query string, and a body over 4 KiB, chunked or not, as the MCP gate does.
- **The run's own directory.** Each run gets a directory (0700) under `/tmp`, holding its MCP config (0600) with the server URL and the token as an `Authorization` header. The token never appears in the command line or the environment, so it stays out of anything the CLI prints. Runs in the same service can still read it, as above. The directory is removed when the run ends, and leftovers are removed when the runner starts.
- **The environment.** The agent process gets only an allowlist: `PATH` and a few locale variables, `HOME` for its service's login volume, the variables `JOTFLOW_AGENT_ENV_ALLOW` names, and a preset's own fixed settings. With a custom command, it also gets its service's one GitHub token as `GITHUB_TOKEN` and `GH_TOKEN`. With a preset it does not: the token is in the 0600 MCP config, for GitHub's MCP server only. JotFlow, Slack, and GitHub variable names cannot be allowlisted.
- **The command.** It comes from a named preset, `JOTFLOW_AGENT_PRESET`, described below. The escape hatch for a CLI without a preset is `JOTFLOW_AGENT_COMMAND`, a JSON list with `{mcp_config}` and `{run_id}` placeholders, run without a shell. A runner takes exactly one of the two. The agent's output is discarded: results come only through MCP tools (D1), and the output may quote Slack text.
- **Ending a run.** A run past its time limit, or one JotFlow stops, loses its whole process group: SIGTERM, then SIGKILL. When the runner itself stops, it stops every live run.
- **JotFlow's side (#21).** JotFlow's run supervisor starts each queued run on the runner for its kind: `JOTFLOW_RUNNER_URL` with `JOTFLOW_RUNNER_SECRET` for drafting and question runs, and `JOTFLOW_RUNNER_EXEC_URL` with `JOTFLOW_RUNNER_EXEC_SECRET` for execution runs. The URLs default to `http://agent:8766` and `http://agent-exec:8766`. There is no fallback between them, and JotFlow refuses to start if the two secrets are equal. It never follows a redirect or uses a proxy from the environment, so a secret or token goes only to its own runner. It polls each run every 2 seconds and ends it through the run lifecycle, which revokes the token first. A run past its deadline is timed out, its token revoked, and then the runner is told to kill it. A run that fails or times out gets a short notice in its thread. The notice is fixed text with the run id only, never the agent's output or the runner's reply. The reason goes in JotFlow's log and the run record.
- **Projects boards (#309).** The execution runner answers `POST /boards/add`, with a strict body of `repo`, `issue_number`, `board` and `proposal_id`, and `GET /boards/{owner}/{number}/access`, with whether its write token may update the board. Both use three constant GraphQL queries, take only validated, typed variables, and fail on errors, a null node, or another shape. The add first reads the issue's body, and refuses an issue without the proposal's marker line, so an execution agent can't get another issue boarded. No agent has a projects tool. JotFlow asks for the add as the thread notice for an applied issue goes out. It does so only for boards still listed and in the approved draft, records each attempt in the tool-call ledger, retries up to three times, and names a board that still fails by its nickname. At start, JotFlow checks each listed board's access and turns off any it can't update: a false `viewerCanUpdate`, a null board, or a not-found or forbidden answer. A check that fails, at a rate limit or a GitHub error, leaves the board on, since the add's own failure is named in the thread.
- **Repo discovery (#236).** For a channel with a scope, the drafting runner answers `GET /repos/{owner}` with one owner's repos, from its read token, and the execution runner answers `GET /repos/{owner}/{name}/access` with only whether its write token can see that repo. That is an early refusal, not proof of write access: the real limit is the write itself, which fails closed at GitHub and reports on the card. Both take path parameters only, checked on both sides, behind the runner's secret. The runner reads at most 1,000 repos and 1 MB, builds each page's URL itself, keeps 8 fields of each repo, and drops a repo under another owner. JotFlow's repo catalog lists each scope owner at start and every 15 minutes, and a miss asks again at most once a minute. A listing older than 24 hours finds nothing, so it fails closed. Every reader of a channel's repos asks the catalog: the drafting packet, `propose_issue`, the card and its repo dropdown, `re:`, the thread reply, help, and rebuild. Approval checks a found repo again, and its write access, caching only a yes, for 15 minutes.
- **Marked issues, for `rebuild --from-github` (#256).** The drafting runner answers `POST /repos/jotflow-issues`, a strict `{owner, name, since}` body with no query string. It pages that repo's issues with its read token, at most 5,000 and 30 MB, drops pull requests, and keeps only issues with a JotFlow marker line. For each, it returns only the number, state, author login, a cleaned title, the marker ids and the parsed Slack source pointers, never the body or comments. The execution runner answers `GET /identity` with its write token's login only, so rebuild trusts only issues JotFlow itself wrote.
- **Who a person is on GitHub, for assigning by request (#305).** The drafting runner answers three fixed reads with its read token, path parameters only, never following a redirect. `GET /accounts/{login}` gives whether the account exists, whether it's a user or an organization, and GitHub's numeric id, which survives a rename. `GET /repos/{owner}/{name}/assignable/{login}` adds whether GitHub would accept it as an assignee there. `GET /orgs/{org}/members/{login}` gives `yes`, `no`, or `hidden` when the read token can't see private memberships, which callers refuse. An app's `name[bot]` login is refused. A rate limit is 503 and any other GitHub failure a fixed 502, so neither ever reads as "no such account". The replies are strict models with nothing else from GitHub, no email or name. The execution runner answers none of them.
- **Start and stop (#55).** `jotflow run` opens the store, recovers interrupted runs, starts the MCP server with the read and write tools, and only then starts the supervisor and the debouncer. Slack connects last. Each run kind is on only when `[agent] preset` is set and that kind's runner secret is present, decided once at startup with one log line for each kind that is off. A kind that is off never starts: its runs stay queued and its proposals stay approved, rather than failing. With drafting off, nothing is queued from Slack. With every kind off, no token is issued. JotFlow then asks each runner that is on for `GET /info`, which is behind the same secret and gate as `/runs` and returns its preset (`command` for a custom command) and the kinds it serves. A runner whose preset differs from `[agent] preset`, that serves other kinds, refuses the secret, or does not answer turns its kinds off in the same way. JotFlow resolves each runner's name once and sends the secret only if every address is private, then connects to that address with the name in `Host` (#234). A public answer is refused at once, a name that doesn't resolve is retried for about 15 seconds, and each call has a 3-second connect timeout. On the way out it runs in reverse. Slack closes, then the supervisor stops live runs on their runners. Every token is revoked before the MCP server stops, and the store closes last. Runs still running stay so in the store, and the next start's recovery queues them again.

**Presets (#22).** A preset is named in the agent's env file, not in `jotflow.toml`, because JotFlow sends the runner only the run id, kind, and token. Adding one is a class in `src/jotflow/adapters/presets.py` and needs no change to JotFlow. A preset loads exactly two MCP servers, JotFlow and the pinned GitHub server, and allows each run kind only its own tools:

| Run kind | JotFlow tools | GitHub tools |
|---|---|---|
| Drafting | `get_work`, `list_topics`, `get_topic`, `propose_issue`, `propose_update`, `revise_proposal`, `link_message`, `ask_channel`, `list_repo_docs`, `read_repo_doc` | read-only: `issue_read`, `list_issues`, `search_issues`, `list_label`, `get_label` |
| Question | `get_work`, `list_topics`, `get_topic`, `ask_channel` | the same read-only tools |
| Execution | `get_work`, `record_result` | `issue_read` and `list_issues` for the marker check, `get_label` for the label check, then `issue_write` for the one create or edit, or `add_issue_comment` for an update's comment |

Each restriction holds in three places. GitHub's MCP server exposes only the kind's tools, and read-only mode wins over its tool list. JotFlow lists only the kind's tools. The CLI itself has no shell, file, or web tools, and refuses any tool not on the list without prompting. The model is set per run kind with `JOTFLOW_AGENT_MODEL` and `JOTFLOW_AGENT_MODEL_<KIND>`. Session transcripts and memory are off, and the CLI's per-server logs stay in the run's directory, so they are removed with it. [presets.md](presets.md) lists each preset's exact flags, what it was verified against, and how to repeat the check after a CLI upgrade.

**The policy file in Compose.** `docker-compose.policy.yml` mounts the team's `jotflow.toml` read-only at `/etc/jotflow/jotflow.toml` and points `JOTFLOW_CONFIG` at it. Start JotFlow with both files, or set `COMPOSE_FILE` so plain `docker compose up -d` uses both:

```
docker compose -f docker-compose.yml -f docker-compose.policy.yml up -d
COMPOSE_FILE=docker-compose.yml:docker-compose.policy.yml
```

The mount is kept out of `docker-compose.yml` because Docker creates a root-owned directory on the host when a bind-mount source is missing. That directory would block `jotflow init` from writing the file. The base file therefore runs without a policy, for `--version` and image checks. The policy holds no secrets, and the container's user must be able to read it, so the file needs mode `0644`. With `0600`, JotFlow stops with a permission error naming the file.

## Distribution

A team never needs the source code. Each tagged release publishes two artifacts built from the same commit and version:

| Artifact | Published to | Used for |
|---|---|---|
| Python package `jotflow` | PyPI | `uvx jotflow init`, the one-time interactive setup on the team's machine |
| Container images `ghcr.io/jnkman787/jotflow` and `jotflow-agent` | GitHub's container registry | `jotflow run`, the always-on service, and the team's agent containers |

`jotflow init` writes `jotflow.toml`, `.env`, and a Compose file pinned to the matching image version, so the whole install is `uvx jotflow init` followed by `docker compose up -d`. The repository is where contributors work; cloning it is only needed to change JotFlow.

How a release is made (#39, #87), from `.github/workflows/release.yml` on a `vX.Y.Z` tag:

- One job checks that the tag, `pyproject.toml`, and `__version__` agree. Its version names both images and the package, so they cannot drift.
- The images are built for amd64 and arm64, published as `:X.Y.Z` and `:latest`, and attested.
- The package job builds the sdist and wheel from the tagged commit. It installs the wheel alone and renders the published Compose files from it, which must pass `docker compose config`.
- Only after the images are published, the PyPI job publishes the package through PyPI's trusted publishing, with OIDC and no stored token, and attests the files' build provenance. It and the image publish job, which signs the images' attestations, are the only jobs holding `id-token: write`.
- The wheel carries the repository's own Compose files and agent env examples. Outside a checkout, `jotflow init` writes them with each `build:` removed and the images pinned to its own version. An existing file is replaced only if the person agrees.

JotFlow is a running service rather than an MCP server listed in an agent's configuration, because it must be connected to Slack when no agent session is open, decides when to start the agent, holds state across days, and serves the whole channel. The MCP connection is how the agent talks to JotFlow while a run lasts.

## Intent keywords

People never need syntax: a plain message is classified by the agent. Keywords let them steer explicitly. A keyword is the first word of a message followed by a colon (D10).

| Keyword | Handled by | Effect |
|---|---|---|
| `idea:`, `bug:` | Agent, as a hint | Suggests the kind of issue. The agent may overrule it; the card says when it does. |
| `re:` | JotFlow | Attaches the message to an existing topic or issue in this channel's repos. `re: 42` when unambiguous, `re: web#42` otherwise. |
| `skip:` | JotFlow | Marks the message as not an idea. Its text is kept out of agent packets. On a top-level message it covers the whole thread. |
| `ask:`, or an @-mention of the bot | Question run | A question answered once in the thread from this channel's topics and from `/jotflow help` (D12, #57). A question run has no GitHub access of its own: its answer posts without approval, and the shared read token reaches repos that feed other channels. Its one GitHub read is `get_issue`, which JotFlow serves through the drafting runner for one issue in the asking channel's repos (#273). Only this workspace's own members can ask. The answer is plain text, labelled as the agent's, with no links, pings, or other repos' references. The asking message is marked as the agent's chatter, so it is never drafted. |

Post-v1: `hold:` and `go:` to lengthen or shorten the wait before drafting, and `new:` to split a reply off into its own topic.

Rules:

- The colon is required, so sentences like "Skip button is broken" are never read as commands. Typos are tolerated only on keywords of four or more letters, and only as two neighbouring letters swapped, such as `skpi:`. A wrong letter is not, because `Ship:` or `Skin:` would then drop a thread.
- JotFlow confirms a recognised keyword with a reaction on the message, so the poster knows it was understood. Reactions never restart the wait before drafting.
- Keywords can have an equivalent reaction, for people who forget or prefer clicking: :no_entry_sign: for `skip:`, :bulb: for `idea:`, and :bug: for `bug:`, listed once in `reactions.py`. `re:` cannot have one, because it needs an issue number. Reactions from guests and external users are ignored. An idea or bug reaction reaches the agent as a hint, like the text keyword, and a keyword in the text wins. Removing a reaction takes its keyword back (#138, #139). JotFlow records who reacted, so a skip is undone only when no one else's skip reaction is left, and never when the message's own text says `skip:`.
- A `re:` that names an issue outside the channel's repos is refused, with a thread reply that says why. One that names an issue with no topic yet starts a topic for it.
- `re:` and `skip:` are stored as the poster's own decision, which the agent cannot overrule. JotFlow applies them before the debouncer joins a reply to its parent's topic, and a skip reaction that arrives later still overrides that join. The debouncer never arms a thread for a `skip:` message, or for any reply under a `skip:` first message, so neither costs an agent run or restarts the wait.
- JotFlow strips the keyword before the agent sees the text, and passes the rest as untrusted thread content. A keyword never adds instructions to the agent's prompt.
- No keyword can skip approval or change the channel mode.
- Acting on someone else's message: with approvers off, anyone may, and JotFlow posts a visible note in the thread. Slack lets only the person who reacted remove the reaction, so they can undo it; the author asks in the thread. With approvers on, only the author or an approver may.
- Editing a message after its card is posted never changes what is approved.
- Help comes from one text (#80). `/jotflow help` shows the set privately, with the channel's repos and how approval works there. It and `/jotflow repos` reply in headed, bulleted sections (#257). They're `mrkdwn` with `verbatim: true`, so Slack links nothing. Every admin-written field is escaped: `&`, `<` and `>` become entities, and `*`, `_`, `~` and backticks become lookalikes. Nicknames and `owner/name` go in code spans. A long repo list splits across sections, within Slack's 3,000 characters a section and 50 blocks a message, and ends "and N more" if it still doesn't fit. JotFlow pins a short guide once in each channel, a person's first message gets a one-time private tip, and a message that looks like a mistyped keyword, such as `skip` or `Idea - ...`, gets one private suggestion. JotFlow never acts on a near miss. Each is recorded in the store before it is sent, so none repeats after a restart, and each can be turned off per channel in `[help]`. Nothing is posted in a paused channel.
- When JotFlow replies with an issue link, it includes the exact handle to use, for example "reply `re: web#42` to add to this", so nobody needs to know issue numbers.

## Channels and repos

Each channel lists the repos it may feed, each with a nickname and a one-line description of what it covers (D11). The agent picks the repo by matching the thread against those descriptions. The card shows the chosen repo prominently with a dropdown to change it before approving. When the agent is unsure, it asks in the thread. A channel with one repo needs none of this.

## Approval gate

Each channel has a mode:

- `propose`: the bot posts a card with Approve and Request Changes. Nothing is written until approved.
- `auto`: the write happens immediately and the same card follows, so anyone can see what was created and adjust.
- `paused`: the bot reads nothing, starts nothing new, posts nothing, and refuses every card action. The one exception: an execution run already running when the channel is paused may still finish its GitHub write; approved runs not yet started wait until it's unpaused. A running one may still call `record_result` (#155).

The mode applies uniformly to creation, description edits, comments, assignment, and labels.

`/jotflow mode` shows the channel's mode privately, and `/jotflow mode propose|auto|paused` sets it (#28). The mode is runtime state in the store (D2). Who may change it follows D7: any workspace member while approvers are off, only an approver while they are on, and never a guest or an external user. Every change is announced in the channel in plain text. Setting `auto` in a channel shared with another organisation, or with a guest among its first 1000 members, adds a warning to that announcement. The scan reads the members, then pages `users.list` once (#161), stopping when every member is found, rather than calling `users.info` per member. It fails closed: a member the directory doesn't list counts as a guest. A channel over 1000 members, a directory too large for one 40-page scan, or a listing that fails or stays rate-limited past a 20-second budget all get the warning anyway.

- **Paused** is enforced in the store: no run starts in a paused channel, and no card is drawn there, until it is unpaused. The orchestrator ignores the channel's messages and reactions, so no keyword, no debouncing, and no help. The card's buttons and the approval reaction are refused. A run already running when the channel is paused cannot write either: its `ask_channel`, `link_message`, `propose_issue`, and `propose_update` calls are refused. Only `record_result` is allowed, because it records a GitHub write the agent already made. So pausing stops new work and every post JotFlow would make. An execution run already running when the channel is paused may still finish its GitHub write and report it, since that write goes through the agent's own GitHub access. Approved runs not yet started wait until it's unpaused.
- **Auto** approves each new proposal as "auto" and queues its execution run, in the card loop. A proposal whose card was already shown when the channel switched is left for a person. The card is posted once the write has finished, applied or failed, and it records "approved by auto mode".

**What approval guarantees, always** (D7):

- The card shows the full issue body, the target repo, labels, assignees, size, and confidence. A public target repo shows a warning. JotFlow has no GitHub access, so visibility comes from the repo's `visibility` line in `jotflow.toml`, which `jotflow init` fills from the installer's own `gh` login when it can, and asks for otherwise. The drafting agent may report `repo_visibility`: its `public` adds the warning, but its `private` never removes one, since agent output is untrusted. With neither source, the card says visibility was not checked (#122). The agent's report is outside the approval hash and never reaches the execution run.
- Approving locks that exact content by hash. The execution run receives only those approved fields.
- The card records who approved and when.
- **The card itself** (`src/jotflow/orchestrator/cards.py`, #26). It shows:
  - the full body, split into sections, and a draft too long to show on one card offers no Approve button
  - the repo, with a dropdown of the channel's repos (D11), and a public-repo warning, or "visibility was not checked" when unknown
  - labels, marked as not checked when the repo has no label list
  - assignees, with unverified identities marked, and the people involved, with one-click assign and remove
  - size, confidence, links to the source messages, and a state line
  - when the agent overruled the author's `idea:` or `bug:` hint, "Hint overruled:" with the agent's one-line reason (#147)
  Agent-written text is always `plain_text`, so nothing in it can ping or link. The Approve button carries the content hash of the draft it shows. The card's message ts is stored, so later states edit the same message.
- **Keeping the card live** (`src/jotflow/orchestrator/card_sync.py`, #152). The store is the card's outbox. Every change a card shows raises the proposal's revision, and the card records the revision it shows. One loop in the orchestrator, about once a second, posts each missing card and edits each one that is behind. So `propose_issue` and the other tool writes never call Slack, and a Slack failure cannot fail them. A card that could not be drawn stays due and is retried, waiting longer each time up to a minute, across restarts too. A proposal superseded or rejected before its card went up gets none.
- **Editing from the card** (`src/jotflow/orchestrator/card_edits.py`, #160). The repo dropdown moves a new-issue proposal to another of the channel's repos, dropping labels the new repo does not allow and the agent's visibility report for the old repo. Make assignee adds a participant's linked GitHub login, and Remove takes a login off the assignees, never off the participants. Who may click is Approve's rule, checked by the approval gate itself, and with approvers on only an approver may edit, a source author included. Only a pending or failed proposal can be edited. An edit changes the content hash, so the card is redrawn and an Approve from before it is refused as out of date. Each edit is recorded with who, when, and which field, never the value.
- Guests and external Slack Connect users can never approve.

**Who may approve, configurable** (D7):

| Setting | Approve | Change mode | Self-approval |
|---|---|---|---|
| Approvers off (default) | Anyone in the channel | Anyone in the channel | Allowed |
| Approvers on | Listed approvers | Listed approvers | Allowed, for a listed approver |
| Approvers on, with `require_second_approver = true` | Listed approvers | Listed approvers | Not allowed |

`require_second_approver` needs an approver list: on its own it would do nothing, so the policy refuses it (#244).

Every mode change is announced in the channel. Approval by reaction is off by default and can be turned on.

**Approval by reaction** (#34). With `[approval] reactions = true`, the emoji named by `reaction`, `heavy_check_mark` by default (not `white_check_mark`, which JotFlow adds to confirm a keyword), counts as Approve when added to a pending proposal's card. It can't be an emoji that already means a keyword, such as `no_entry_sign`, or one JotFlow adds itself, and it's ignored while `reactions` is off. The reserved names live in `src/jotflow/reactions.py`, so a keyword reaction added later is refused too. A reaction carries no hash, so it approves only a card that shows the stored draft: drawn at the proposal's current revision (#152), and shown in full, the same rule that decides whether the card has an Approve button. Every other check is the button's: this workspace, no guests or external users, the proposal's own card, the approver list, and authorship. Slack has no payload team for a reaction, so `users.info` decides whether the person is external. A reaction on any other message, another emoji, or reactions turned off change nothing and get no reply.

**Request Changes** opens a dialog whose text JotFlow posts as a thread reply attributed to the person, so the next drafting run treats it like any other message (D9). The reply reads `Change request from @person:` followed by the text, cleaned as `ask_channel` text is, with broadcast mentions removed, Slack markup escaped, and nothing unfurled. Ingest never sees JotFlow's own messages, so JotFlow starts the thread's quiet window itself.

**How a click is checked** (`src/jotflow/orchestrator/approval.py`, #27). JotFlow runs in Socket Mode, so there is no request signature to verify: Bolt receives interactions over the websocket it opened with the app-level token, and only Slack can send on it. On top of that, every Approve, Retry, Discard, or Request Changes click is checked in this order, and any refusal is a fixed reply only the person sees, writing nothing:

1. The payload's team, and the person's own team, are this workspace.
2. The person is not a guest and not an external Slack Connect user.
3. The proposal exists in the card's channel, and the clicked message is its stored card.
4. With approvers on, the person is a listed approver. With `require_second_approver` too, for Approve or Retry they must also not have written the thread's first message or any message the proposal cites, as Slack reports them at the moment of the click. Someone who was only mentioned may approve. If Slack can't confirm an author, the approval is refused.
5. Approve and Retry lock the content hash the button carries. A stale card approves nothing and is redrawn.

An approval records who and when, queues one execution run, and redraws the card. A failed card offers **Retry**, which approves again and starts a fresh count of execution attempts (#30), and **Discard**, which clears the draft, after which Retry is refused.

**What the gate is and is not.** JotFlow enforces the gate in its own flow: it never hands the agent a proposal to execute until the mode or a person allows it, and it validates proposals against the configured repos, and against a repo's label list where one is set, before posting a card. It is not a credential boundary: the agent holds the team's GitHub access, so a misbehaving agent could still write on its own. D6 narrows this: drafting runs, the ones exposed to raw Slack text, get read-only GitHub access. Teams should still scope the agent's write token to the repositories JotFlow is meant to touch.

**Open mode, stated plainly.** With approvers off, the default, the gate catches agent mistakes but not a single member acting alone: anyone can post text, approve it themselves, or switch the channel to `auto`. In `auto` mode, text from anyone in the channel reaches GitHub with no human review. That fits a small team of equals. Channels with wide membership, guests, or external Slack Connect users should turn approvers on. JotFlow warns in the channel when `auto` is set in a channel that has guests or external members.

## Conversation rules

Defaults that need no syntax (D9):

- A loose top-level message starts a candidate topic. The agent may attach it to an existing one.
- A reply in a thread whose topic has expired revives that topic: the reply joins its top-level message's topic, which refreshes the topic's activity, unless someone already placed it.
- A thread is drafted only after it has been quiet for its channel's `debounce_seconds` (120 by default, overridable per channel), so the first draft reflects a whole burst. Each loose top-level post is its own thread with its own timer and run; posts are never grouped by time or author. Only accepted messages re-arm a timer, so the bot's own messages never do (`src/jotflow/orchestrator/debounce.py`). Each waiting thread's due time is also kept in the store's `pending_threads` table, holding only the channel id, the thread ts, and the time. So a restart forgets no thread: startup re-arms them after recovery, and a window that ended while JotFlow was down flushes at once. A thread whose top-level message a person marked as chatter is dropped instead. Queuing the drafting run and forgetting the thread happen in one transaction, so a thread is never queued twice (#130).
- Expiry only removes a topic from the active index handed to the agent. Its record, a few ids linking messages to an issue, is kept indefinitely so ideas revisited months later still find their issue. Expiry is by quiet period only, after `topic_window_days`. A message linked to an expired topic, through `link_message` with its id, puts it back in the index, and `get_topic` still reads it (#37). A closed issue doesn't expire its topic: JotFlow can't see GitHub's state (D5).
- A follow-up reply in a thread with an issue produces an update proposal, gated like any other.
- The agent sees the thread in question plus short excerpts of likely-related threads, so it can correlate on content rather than titles alone.

## Correlation

This is the novel and risky piece. The agent, not the orchestrator, decides where a message belongs, but the orchestrator constrains the decision.

- Every packet includes the channel's active topic index and related-thread excerpts. The index lists topics with a message linked in the last `topic_window_days` (14 by default), most recently active first and capped at 50. Each entry has its id, title, linked issue, message count, and up to five thread timestamps. It travels as compact JSON with a note that titles were written by earlier runs and are untrusted data (`src/jotflow/orchestrator/topics.py`).
- The agent classifies each new message as: new topic, belongs to topic X, or chatter, and records any keyword hint it overruled and why.
- Below a confidence threshold the agent uses `ask_channel` rather than guessing.
- People override with `re:`, `skip:`, or the equivalent reaction; overrides are recorded so the index stays correct.
- **Reuse before creating.** Before proposing a new issue, the agent looks for an existing home for the idea in two places, regardless of age:
  - GitHub: open and closed issues across all of the channel's repos, searched with its read-only access.
  - JotFlow's topic history: every topic and the issue it became, including expired topics.
- When it finds a likely match, the agent proposes an update to that issue, or asks in the thread whether the idea is the same, instead of drafting a new issue. A closed match is shown as such so a person can decide whether to reopen it or start fresh.
- The card shows the best candidate prominently with a one-click "this is the same as" action, so a duplicate costs one click to resolve.
- This search is separate from the execution-run marker check, which only guards against a crashed run creating the same issue twice within minutes.

## Run lifecycle

Every agent run is recorded in the team's own database (D8).

- A run has a kind (drafting, execution, and from M5 question), a status, a deadline, and its linked proposal. Its timeout is at most two hours, the longest an MCP token lasts, so no run outlives its token.
- The database enforces the per-thread rule, so it holds across a restart or a second process. A partial unique index allows one running run per thread. A top-level post is its own thread: its run carries the post's own ts, which is also the `thread_ts` Slack gives every reply to it, so a post and its replies share one lock. A burst during a run coalesces into one queued follow-up. An approved proposal has at most one queued or running execution run.
- At most one run per thread at a time, of any kind, including execution. A follow-up reply arriving while an issue is being created waits until the issue number exists, so it can become an update proposal rather than a second new issue. Messages arriving during a run trigger a fresh run afterwards, and the newer proposal supersedes the older pending one.
- A total concurrency cap, `[agent] max_concurrent_runs` in `jotflow.toml` (default 2), limits how many agent processes run at once. The oldest queued run whose thread is free starts first.
- Each run gets exactly one MCP token while it may act (#107). `RunLifecycle` in `src/jotflow/orchestrator/runs.py` issues it when the run is claimed, lasting until the run's deadline plus 60 seconds and never past the two-hour cap, so a run at the full two hours gets no grace. An execution run moves its approved proposal to executing as it starts, which `record_result` requires; a reconciling proposal stays reconciling, and any other state fails the run without a token. An execution run that ends with its proposal still executing, by any exit including a clean one, did not record a result, so the proposal moves to reconciling and the next attempt checks for the marker first. JotFlow retries it in process, still reconciling, after 30 seconds and then 2 minutes. At the third ended attempt, with interrupted runs counted, the proposal fails. Its card shows the fixed failed line, or the thread gets one fixed reply, and the agent's reason stays in the store. The limit also holds after a restart: a run recovery queued for a proposal already at the limit fails it without starting. A person can retry a failed proposal by approving it again (#30). It revokes the token before recording any end: a finish, a timeout, or recovery at startup. Tokens are never stored or logged.
- On restart, every queued or running run is marked interrupted. Each thread with an interrupted drafting run gets one fresh drafting run. Every proposal that is approved, executing, or reconciling then gets one fresh execution run, whether or not its last run was recorded. That also covers a crash between approving a proposal and queuing its run. A proposal that was executing moves to reconciling first, through the same transition rules as everywhere else, so the next attempt checks for the proposal's hidden marker before writing. A proposal still approved never started its write and stays approved. Any other proposal gets no new run.
- Run states:

```mermaid
stateDiagram-v2
    [*] --> queued: enqueued, or coalesced with one already queued
    queued --> running: claimed under the cap, thread free
    queued --> failed: cancelled
    running --> succeeded
    running --> failed
    running --> timed_out: past its deadline
    queued --> interrupted: JotFlow restarted
    running --> interrupted: JotFlow restarted
```

- Proposal states and transitions:

```mermaid
stateDiagram-v2
    [*] --> pending: drafting run proposes
    pending --> approved: Approve, or automatically in auto mode
    pending --> rejected: rejected or skipped, or Same as an existing issue (#36)
    pending --> superseded: newer proposal on same thread
    approved --> executing: execution run starts
    executing --> applied: record_result with matching URL
    executing --> reconciling: timeout, crash, or restart
    reconciling --> applied: marker found in repo
    reconciling --> reconciling: retried under the limit, marker checked first
    reconciling --> failed: retry limit reached, or the agent reports an error
    executing --> failed: agent reports an error
    failed --> approved: a person retries from the card, while the draft is kept
    applied --> [*]
```

In `auto` mode a validated proposal moves from pending to approved without a person, and the card records "approved by auto mode".

**Follow-ups revise a pending draft (#304).**
- **The packet:** a drafting run's packet gives each topic its thread feeds that topic's live draft, as `pending`, with its revision and its text marked untrusted.
- **The tool:** `revise_proposal` replaces that draft with a whole revised one, plus the new messages as sources. It runs `propose_issue`'s and `propose_update`'s own checks, and #209's doc-passage check, and reads who wrote the new messages from Slack.
- **The store** applies it only while the proposal is pending and still at the revision the run read. The repo, topic and issue number stay the proposal's own, and the sources and participants merge with the old ones.
  - A revision that changes nothing shown is refused.
  - A proposal takes at most 10 revisions, and a run at most 3, apart from its proposal cap.
  - `proposal_revisions` and `proposal_revision_authors` keep which fields changed and whose messages, one row per person, never the text.
- **A stale revision,** after a person's card edit or another run's revision, returns the current draft for one retry in the same run.
- **A revision that lost a race** to an approval, a rejection or a write sets `follow_up_due_at`. The card loop then drafts the thread again once the proposal is applied, rejected or superseded, so a quiet thread doesn't keep a lost addition.
- **The card** says who revised it and what changed, and marks each changed field. The marks sit outside the hash, so the Approve button still locks exactly the draft shown.
- **Reactions:** once a pending card has changed in place, by a revision or a person's card edit, a reaction can't approve it. A reaction names only the message, not the draft it showed, so it could approve a draft the reactor never read. The reactor gets a private reply to use the button, which carries the hash. A never-changed card approves by reaction as before.
- **Re-drafts after a lost race** also start when a failed proposal is discarded. A topic whose latest proposal was rejected is marked `rejected` in the packet, so the re-draft doesn't just propose it again.
- **After filing,** a follow-up is a `propose_update`, a comment by default. Its header says whether it's a comment or a change to the description.

**`rejected` with `duplicate` set means "same as an existing issue", not "people said no" (#36).** A drafting run may list up to three `related` issues, as pointers: repo, number, and the open or closed state its search reported. They must be in the channel's repos. The card shows them, labelling the state as the agent's claim, and offers **Same as** for the first one on a pending new issue. A click under Approve's who-may rules then does three things in one store write. The proposal becomes `rejected`, with `duplicate` recording the issue, who clicked and when. The topic is bound to that issue. The draft is cleared. There's no execution run and no GitHub write. Later messages in the thread become `propose_update` on that issue, which may be closed. **Not a duplicate** takes it back under the same rules: it unbinds the topic, records who undid it, and queues a fresh drafting run. It's refused if the topic now points elsewhere, or if a proposal drafted after the click is still pending, approved, executing, reconciling or applied, because that proposal relies on the binding. The store keeps the four duplicate columns consistent, all set together and only on `rejected`, because SQLite can't add that CHECK to an existing table.

## Data and privacy

JotFlow stores pointers and derived metadata, never Slack message bodies.

Stored, in the team's own database:

- channel id, message timestamp, thread timestamp, mapped to a topic id
- topic id mapped to a repo and issue number, plus a short generated title
- proposals: structured fields, state, approver, content hash
- run records, identity links, and per-channel settings

Not stored by JotFlow: message bodies, channel history.

The store (`src/jotflow/store/`) enforces this. Every column carries a comment saying what it holds, and a test reads the live schema and fails if any column is named or documented as a message's body, text, or content. The proposals table holds the agent's structured draft, the issue title and body that a person approves, because approval must lock exactly what the card showed (D7). That draft is derived content bound for GitHub, not a copy of the thread. Topic titles are cleaned on every write: no markup, links, or mentions, and at most 80 characters, because a title written by one run is read by others.

**Retention of drafts (#102).** A draft can quote what people wrote, so it stays only while it is needed. When a proposal is applied, rejected, or superseded, its drafted title, body, and comment, and the agent's reason for overruling a keyword hint, are removed in the same transaction; an applied issue's text already lives on GitHub. A failed proposal keeps its draft so a person can retry it, until the draft is cleared explicitly, after which a retry is refused. What stays is the audit trail: the content hash of what was approved, the approver and time, the state, the result and issue URL, the Slack pointers, the participants, and the non-text fields such as repo, labels, and assignees. The database runs with `secure_delete`, so removed text is overwritten rather than left in free pages, and after every clear the store checkpoints and truncates its write-ahead log, so no earlier copy of the page stays in the `-wal` file while JotFlow runs.

Thread text is fetched from Slack when building a packet, passed to the agent, and discarded. Logs never carry it: JotFlow's own modules log ids and timestamps only, libraries that print whole payloads are capped at INFO, and every line passes one redacting formatter, which also covers tracebacks. That formatter removes the Slack payload fields that carry what people wrote, `text` and the attachment, unfurl, and file fields such as `fallback`, `title`, and `comment`, and, always, every token shape JotFlow knows: Slack, GitHub, JotFlow run tokens, bearer values, and private keys. `jotflow run --log-bodies` shows message text for local debugging, with a warning, and is refused in CI and inside JotFlow's image; tokens stay redacted even then.

How the work packet treats thread text:

- It reads only the run's own thread, with `conversations.replies`, never channel history. A very long thread is paged in full, up to a bound, keeping its first and newest messages.
- Related excerpts are the first message of a few other threads already named in the topic index.
- Authors and `<@U…>` mentions become display names, with the Slack user id kept alongside. The only cache maps user ids to names: directory data, bounded and short-lived, never message text.
- `skip:` messages, a whole thread under a `skip:` root, and messages a person marked as chatter never reach the agent. Keyword prefixes are removed from the text and passed as a hint.
- Very long threads are trimmed to the root, the first replies, and the most recent ones, within fixed size caps.
- Slack rate limits are retried with backoff that honours `Retry-After`. One `get_work` call shares a single 20 s wait budget across every Slack call it makes: the thread's pages, name lookups, and related excerpts. That keeps it inside the MCP server's 30 s request timeout. If the thread cannot be read within the budget, the agent gets a clear error and nothing is recorded as served. A name lookup that cannot finish shows the user id instead, and related excerpts that cannot be fetched are left out.

The local store is a **rebuildable cache**. Each linked Slack message receives a reply with the issue URL, and by default each issue carries the Slack permalinks it came from, so the correlation map can be reconstructed from Slack alone. Setting `issue_links = "none"` keeps permalinks and every other workspace detail out of issue bodies. That holds for the drafted text too. With it set, `propose_issue` and `propose_update` refuse a title, body or comment holding a link to any `slack.com` host, or a `slack://` link, with a fixed reason the agent can act on (#168). The text is checked after percent-decoding, compatibility folding and removing invisible characters, so an encoded or hidden domain is refused too. The drafting instructions already tell the agent to leave out people's names and the channel in that mode. Names in free text can't be checked mechanically, so that part rests on the instructions and on the person who approves.

Where conversation content does go, so teams can judge it:

- **The agent's model provider.** If the agent uses a hosted model, thread text goes to that vendor under the team's own agreement. JotFlow sends only the thread in question and short excerpts of related threads, never channel history.
- **GitHub.** Issue bodies summarise the conversation they came from. If the target repo is public, that summary is public; the card warns when it is.
- **The agent's host.** Some agent CLIs save session transcripts. Presets turn this off where the CLI allows it; where it does not, transcripts stay in the agent container.

There is no hosted component and no telemetry. Nothing is sent to the JotFlow project.

## Permissions

| Component | Needs | Does not need |
|---|---|---|
| JotFlow | A Slack app installed in the team's workspace: `channels:history`, `groups:history`, `channels:read`, `groups:read`, `chat:write`, `reactions:read`, `reactions:write`, `pins:write`, `users:read`, `commands`, and from M5 `app_mentions:read`, plus an app-level token for Socket Mode | Any GitHub access |
| Team's agent | Two GitHub tokens: a read-only token for drafting runs and a write token for execution runs, both scoped to the channel's mapped repos | Any Slack access |

Each team creates a dedicated Slack app for JotFlow from the manifest in this repository, inside their own workspace. The bot's display name and icon are theirs to choose; "JotFlow" is only the manifest default. The app stays internal to that workspace and is never distributed. Reusing an existing app is not supported: scopes, event subscriptions, and slash commands would have to be merged by hand and would collide with whatever the app already does. Slack requires two manual steps that no software can skip, creating the app and clicking Allow on the install screen, so `jotflow init` wraps everything around them: it renders the manifest, opens the create-from-manifest link, validates the pasted tokens, and writes the environment file. When the team picks an agent preset, it also writes the two agent env files, each with its own runner secret, and names the preset in them (#126).

JotFlow must be a Slack app because something has to be woken when a message is posted. Slack's MCP server is pull-only: an agent that is already running can read, search, or post through it, but nothing in it fires on a new message. Only an installed app can subscribe to events. The agent needs no Slack access because JotFlow fetches thread text, hands it over through `get_work`, and does all posting itself.

## Configuration

Two tiers, because every team supplies every value and the two kinds change at different speeds.

| Tier | Lives in | Changed by | Holds |
|---|---|---|---|
| Policy | `jotflow.toml`, deployed with the service | An admin editing the file | Each channel's repos with nickname and description, optional label lists, optional approver list, reaction approval, `issue_links`, debounce window, topic window, agent preset and run concurrency cap |
| Runtime | The local database | Slack slash commands | Channel mode (`propose`, `auto`, `paused`), Slack to GitHub identity links |

The policy file is validated in full at startup. An unknown or misspelt key is an error, and every error names its key, such as `channels.C0123ABCD.repos[0].nickname`. Channels are keyed by Slack channel id; each lists its repos. A repo's `labels` list is optional: without one, a proposal may use any label that already exists on the repo, and a list narrows that set. An explicit empty list is an error, because it could mean either; leaving the key out is the only way to say "any". Lists are per repo because labels are per repo on GitHub. JotFlow holds no GitHub credentials, so it cannot see which labels exist. The agent's rule is to never invent a label: drafting runs read the repo's labels with read-only access, and the execution run checks each label exists before applying it, since GitHub would otherwise create a missing one. That is a prompt rule, not something JotFlow enforces. JotFlow enforces a label list where a team has set one, and always rejects label names with control characters, line breaks, bidirectional overrides, invisible format characters such as zero-width spaces, or surrounding spaces. An empty approver list means approvers are off. [`jotflow.example.toml`](../examples/jotflow.example.toml) is a complete, commented example. The environment tier holds only the Slack tokens, the MCP host and port, `JOTFLOW_MCP_ALLOWED_HOSTS` (extra `Host` names for the MCP server, such as the Compose service name), the database path, the two runner URLs and secrets, and `JOTFLOW_CONFIG`, the path to the policy file.

Setup is a first-class feature: `jotflow init` asks a few questions, checks the Slack tokens, and writes `.env` and `jotflow.toml`, and the Slack app manifest installs in a couple of clicks. The written `jotflow.toml` is checked by the same loader `jotflow run` uses, so it always loads. A team should go from clone to connected bot without reading source.

## Identity

Assignment requires a Slack user to GitHub handle mapping. A self-service slash command lets each person link their own account. Links are self-declared in v1, and cards mark them as unverified. `/jotflow link <login>` links the caller only, since it takes no user argument. The login must match GitHub's rule, and `@login` or a pasted profile link also works. `/jotflow link` shows the caller's link, and `/jotflow unlink` removes it. Guests and external Slack Connect users can't link, and a login someone else holds is refused without naming them. The store keeps only the Slack id, the login and the time, and the log keeps the Slack id and the action (#35). An assignee must be the linked login of one of the proposal's participants (#15). Because links are self-declared, someone can claim another person's login first. The card's "(unverified)" shows that. An approver, or someone in `[identity] admins`, removes a link with `/jotflow unlink <member-id>`, and whoever runs JotFlow can use `jotflow identity unlink <member-id>` on the host (#173). The target is named by Slack member ID or mention, never by name, and the reply shows their display name, so a mistyped ID is obvious. The person is told privately in the app's read-only Messages tab. The store records whose link was removed, by whom, and when, never the login or any text. A person proves a link is theirs with `/jotflow verify` (#195). JotFlow gives them a one-time code, shown only to them and stored as a digest bound to their Slack id and the login, for 30 minutes, single use. They put it in their GitHub profile bio or a public gist's description, and `/jotflow verify check` asks the drafting runner to look. The runner reads only the public profile and the 10 most recent gists, anonymously, with no token, so JotFlow's own container never talks to GitHub. It hashes each code-shaped word it finds and compares, and answers only whether it matched, with GitHub's numeric id and canonical login. A verified link drops "(unverified)" from cards. Proof of a login someone else linked moves the link to the prover, tells the other person privately, and records the move as removed by `verification`. Relinking or unlinking drops verification. A rate-limited GitHub answer is "try again later", never "not verified".

## Security posture

The full picture, by adversary and with the accepted v1 gaps, is in [threat-model.md](threat-model.md).

- Slack content is untrusted input. It reaches only drafting runs. The shipped presets configure drafting runs with read-only GitHub access and no shell or write tools, and execution runs, which can write, receive only the approved structured fields, never the thread itself; because the approved body was drafted from Slack, the execution prompt treats it as content to write, never as instructions. This holds only when the team gives the agent separate read and write GitHub tokens, and only for the shipped presets; a custom command template is the team's responsibility.
- The agent runs in its own container with an environment allowlist and no access to JotFlow's secrets.
- Proposals are validated against the configured repos, and against a repo's label list where one is set, before they can be approved, and a reported issue URL must point at the approved repo.
- `ask_channel` posts only in the source thread, strips broadcast mentions such as `@channel`, and is rate-limited.
- MCP tokens are scoped to their run kind and channel, and an operator token to its channels and the two read tools; the server validates `Host` and `Origin`.
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
| MCP | the official `mcp` package (2.x), its low-level `Server` over Streamable HTTP | Tool schemas derive from the same Pydantic models used everywhere else. Not `MCPServer`, the 2.x name for FastMCP: it always registers prompt, resource, and subscription handlers, so the server would advertise capabilities JotFlow must not offer. |
| GitHub | none | JotFlow's code never uses GitHub credentials. The team's agent performs reads and writes with its own tooling. |
| State | SQLite via aiosqlite | Pointers and run records. Postgres stays possible behind the same interface. |
| Tooling | uv, ruff, mypy strict, pytest | Locked dependencies, one formatter, typed contracts. |

The Pydantic models in `src/jotflow/models.py` are the single source of truth for the MCP tool contract, the validation applied before a card is posted, and the shape rendered into Slack approval cards.

Package layout:

```
src/jotflow/
  models.py       shared contracts: proposals, runs, classifications, topics, channel modes
  config.py       environment-driven settings, no secrets in the repo
  policy.py       jotflow.toml schema, loader, and allowlist lookups
  cli.py          entry point
  orchestrator/   event ingest, keywords and their handler, debouncing, topic index, run lifecycle, approval cards
  whiteboard/     the MCP server and token scoping
  adapters/       agent runners and presets
  execution/      execution runs and result checks
```

The major agent CLIs install through npm, so the agent container carries Node. The JotFlow container does not need it.
