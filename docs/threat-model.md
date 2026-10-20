# Threat model

What JotFlow protects, from whom, how each defence is enforced, and what v1 deliberately accepts. It describes the code as it is. Where this page and the code disagree, the code wins and this page is wrong. Please report that, as a security issue if it matters (see [SECURITY.md](../SECURITY.md)).

Decisions are cited as D1 to D13 from [roadmap.md](roadmap.md). Design detail lives in [architecture.md](architecture.md), and this page links to it rather than repeating it. How to run JotFlow so these defences hold, including backups and other orchestrators, is in [deployment.md](deployment.md).

## What JotFlow is, in one paragraph

JotFlow reads chosen Slack channels, and after a quiet period hands a thread to the team's own agent, running in its own container, as a **drafting run**. The agent proposes a GitHub issue through JotFlow's MCP tools. JotFlow shows the proposal on a card. When a person approves it, JotFlow starts an **execution run**, in which the agent writes the issue with the team's own GitHub token. JotFlow itself never holds GitHub credentials (D5).

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Slack bot token (`xoxb-`) and app token (`xapp-`) | JotFlow's `.env`, mode 0600 (`jotflow init`) | Read the configured channels, post as the bot, and receive events |
| GitHub read PAT | `agent.env`, in the `agent` service only | Read issues and labels |
| GitHub write PAT | `agent-exec.env`, in the `agent-exec` service only | Create and edit issues in every repo it reaches |
| Runner secrets, one per runner | `.env`, plus the matching agent env file | Start runs on that runner |
| Per-run MCP tokens (`jfr_…`) | JotFlow's memory as SHA-256 digests; the run's 0600 MCP config | Act through JotFlow's tools as one run, within one channel and thread |
| Operator tokens (`jfo_…`) | JotFlow's database as SHA-256 digests; the person's own environment | Read topic titles and message pointers in the token's channels, until it expires or is revoked |
| Thread text | Slack only. JotFlow fetches it per run and never stores it | People's words, including private channels |
| Drafts: title, body, comment | JotFlow's database, until the proposal is final | Derived from thread text, and what gets published |
| The approval | The proposal's state and content hash, plus approver and time | The gate between untrusted text and a GitHub write |
| The model API key or login | The agent services' env files or home volumes | Spend, and the team's model account |

## Trust boundaries

```
 Slack ──Socket Mode──▶ JotFlow ──runner HTTP──▶ agent / agent-exec ──GitHub MCP──▶ GitHub
   ▲                      │  ▲                        │
   └── cards, replies ────┘  └─── MCP tools (per-run token) ◀──┘
```

1. **Slack to JotFlow.** Events arrive over the Socket Mode websocket JotFlow opens with the app token. There's no public HTTP endpoint and no signing secret to check. Everything in an event is untrusted.
2. **JotFlow to the agent containers.** These are internal Compose networks with no published ports. JotFlow calls each runner with that runner's own secret. The agent calls back to JotFlow's MCP server with a per-run token.
3. **The agent to GitHub.** The agent uses the team's own tokens. JotFlow can't see or limit that traffic. It can only decide what the agent is asked to do and what it accepts back.
4. **The installer's machine, during setup.** `jotflow init` writes the secrets, and may ask the installer's own `gh` for a repo's visibility.
5. **A terminal agent, with an operator token.** It starts `jotflow operator connect` inside JotFlow's container with `docker compose exec`, on the host or over ssh. That bridge forwards MCP over stdio to the server on loopback, with the token. No port is published for it.

## Adversaries and defences

### A channel member

Anyone who can post in a watched channel, which with approvers off includes the person approving.

| Threat | Defence | Enforced in |
|---|---|---|
| Steer the agent with instructions in a message | Thread text reaches only drafting runs, as data in `UntrustedText` fields. With the shipped presets, drafting runs have read-only GitHub access and no file or shell tools. A custom command can have them (see [Accepted in v1](#accepted-in-v1)). Every write needs a person's approval | [read_tools.py](../src/jotflow/whiteboard/read_tools.py), [presets.py](../src/jotflow/adapters/presets.py), D6, D7 |
| Get a write outside the team's repos or labels | `propose_issue` and `propose_update` accept only the channel's repos and, where set, its label list. The reported issue URL must match the approved repo | [write_tools.py](../src/jotflow/whiteboard/write_tools.py), [policy.py](../src/jotflow/policy.py), D11 |
| Change a draft after someone approved it | The approval locks the draft's content hash. A stale card, or a changed draft, is refused, and an execution run gets exactly the approved fields or nothing | [store/hashing.py](../src/jotflow/store/hashing.py), [approval.py](../src/jotflow/orchestrator/approval.py), D7 |
| Approve their own proposal | With approvers on, only listed approvers may approve, and never someone Slack shows wrote the thread's first message or a cited source | [approval.py](../src/jotflow/orchestrator/approval.py) `_source_authors`, D7 |
| Ping the whole channel, or GitHub users, through the bot | `ask_channel` strips broadcasts, escapes markup and never unfurls. Footer names lose `@`, entities and markup. Change requests are posted the same way | [write_tools.py](../src/jotflow/whiteboard/write_tools.py) `clean_question`, [execution/writes.py](../src/jotflow/execution/writes.py) `clean_name` |
| Flood runs or tokens | One queued drafting run per thread, a concurrency cap, per-run tool caps, and a bounded token registry | [store](../src/jotflow/store/), [tokens.py](../src/jotflow/whiteboard/tokens.py), [write_tools.py](../src/jotflow/whiteboard/write_tools.py) |
| Act in a paused channel, or change the mode | A paused channel starts nothing new, writes nothing, and refuses every card action. The one exception: an execution run already running when the channel is paused may still finish its GitHub write; approved runs not yet started wait until it's unpaused. A running one may still call `record_result` (#155, [architecture.md](architecture.md) "Approval gate"). With approvers on, only approvers change the mode, and guests and external users never do | [modes.py](../src/jotflow/orchestrator/modes.py), [approval.py](../src/jotflow/orchestrator/approval.py) |

### A guest or an external Slack Connect user

| Threat | Defence | Enforced in |
|---|---|---|
| Approve, retry, discard, request changes or react-approve | Refused. The payload's team and the person's team must be this workspace, and `users.info` must not mark them a guest or external. If JotFlow can't learn its own team, everyone counts as external | [people.py](../src/jotflow/orchestrator/people.py), [approval.py](../src/jotflow/orchestrator/approval.py) |
| Link a GitHub login, to become assignable | Refused by the same check | [identity.py](../src/jotflow/orchestrator/identity.py) |
| Skip someone else's message | Refused by the same check | [keyword_handler.py](../src/jotflow/orchestrator/keyword_handler.py) |

### A prompt-injected drafting agent

The likeliest attacker: the agent reads untrusted text, and might do what it says.

| Threat | Defence | Enforced in |
|---|---|---|
| Use GitHub write access | The drafting service holds only the read PAT. The write PAT exists only in `agent-exec`, a separate service and network the drafting container can't reach | [docker-compose.yml](../docker/docker-compose.yml), CI's Compose policy and reach checks in [ci.yml](../.github/workflows/ci.yml), D6 |
| Act beyond its run | Its MCP token is bound to its run, kind, channel and thread. It's revoked the moment the run ends, and kept only as a digest | [tokens.py](../src/jotflow/whiteboard/tokens.py), [runs.py](../src/jotflow/orchestrator/runs.py), D13 |
| Link or propose for another thread | `link_message` and `propose_*` accept only messages the run's own packet served | [write_tools.py](../src/jotflow/whiteboard/write_tools.py), [store](../src/jotflow/store/) `packet_ref` |
| Read Slack or JotFlow secrets | The agent's environment is an allowlist: Slack, JotFlow and GitHub variable names can't be added. It never sees `.env`. The token reaches it only in a 0600 file, never in argv or the environment | [runner.py](../src/jotflow/adapters/runner.py) `child_env`, CI's "No Slack variable inside the agent container" |
| Name uninvolved people in a GitHub issue | The footer names only people Slack confirms wrote the cited sources. The agent's `participants` list never decides | [execution/attribution.py](../src/jotflow/execution/attribution.py) |
| Leak the workspace under `issue_links = "none"` | Drafted text with any Slack link is refused, after decoding and removing invisible characters. JotFlow's footer is the marker alone | [write_tools.py](../src/jotflow/whiteboard/write_tools.py) `_check_no_slack_links`, [execution/writes.py](../src/jotflow/execution/writes.py) |
| Assign a stranger | An assignee must be the linked login of one of the proposal's participants | [write_tools.py](../src/jotflow/whiteboard/write_tools.py) `_check_assignees` |
| Hide something from the approver | Every agent-written field on the card is `plain_text`, and a draft too long to show in full can't be approved, by button or by reaction | [cards.py](../src/jotflow/orchestrator/cards.py) |
| Silence the public-repo warning | Only the policy can say private. An agent's "private" never removes a warning | [card_sync.py](../src/jotflow/orchestrator/card_sync.py) `effective_visibility` |

### A misbehaving execution agent

It holds the write PAT, so JotFlow can't stop it writing. JotFlow can only narrow what it's asked to do and check what it reports.

| Threat | Defence | Enforced in |
|---|---|---|
| Write something other than the approved issue | JotFlow plans the exact writes. The packet holds only the approved fields, never the thread, and the instructions treat them as content to copy | [execution/writes.py](../src/jotflow/execution/writes.py), [execution.md](../src/jotflow/whiteboard/instructions/execution.md), D5 |
| Write to an unrelated issue or repo | The preset allows only the planned GitHub tools. `record_result` must name the approved repo and issue. **The write PAT is the real limit:** scope it to exactly the configured repos, with Issues read and write only | [presets.py](../src/jotflow/adapters/presets.py), [docs/presets.md](presets.md) |
| Write twice after a crash | The execution instructions require the agent to check for the proposal's hidden marker before every write. JotFlow can't see the agent's GitHub calls, so it can't enforce that. JotFlow's own backstop limits the damage: one proposal has at most one live execution run, a retry stays reconciling so the next run checks the marker first, attempts are capped, and a result is recorded once. A duplicate the agent writes anyway is visible on GitHub, but JotFlow can't undo it | [runs.py](../src/jotflow/orchestrator/runs.py), [read_tools.py](../src/jotflow/whiteboard/read_tools.py) |

### Malicious repo or GitHub text

Issue bodies, comments, and public repo files, through Codex's resource tools, all read by the agent.

| Threat | Defence | Enforced in |
|---|---|---|
| Instructions in an existing issue or a public search result | The same as Slack text: the drafting rules treat everything read as data, writes are planned by JotFlow and approved by a person, and the read PAT can't write | [drafting.md](../src/jotflow/whiteboard/instructions/drafting.md), [presets.py](../src/jotflow/adapters/presets.py) |
| Shadow JotFlow's tools through another MCP server | Presets load only JotFlow and the pinned GitHub server, with strict MCP config | [presets.py](../src/jotflow/adapters/presets.py), [Dockerfile.agent](../docker/Dockerfile.agent) |

### A compromised dependency or image

| Threat | Defence | Enforced in |
|---|---|---|
| A changed base image or tool | Base images are pinned by digest, the GitHub MCP server by version and sha256 per architecture, and Python packages by `uv.lock` | [Dockerfile](../docker/Dockerfile), [Dockerfile.agent](../docker/Dockerfile.agent), `uv.lock` |
| A moved Action tag | Every Action is pinned to a commit SHA, Dependabot proposes updates, and zizmor checks each SHA against its repo | [ci.yml](../.github/workflows/ci.yml) "Workflow lint", `.github/dependabot.yml` |
| A tampered release | Release images carry signed build provenance. `gh attestation verify` checks it | [release.yml](../.github/workflows/release.yml), [install.md](install.md) |
| Fork code with secrets | CI runs fork PRs with a read-only token and never uses `pull_request_target` | [ci.yml](../.github/workflows/ci.yml) |

### A person with an operator token, or their terminal agent

An admin issues the token with `jotflow operator create` for named channels. Whoever holds it, including a prompt-injected terminal agent, gets exactly what the token allows.

| Threat | Defence | Enforced in |
|---|---|---|
| Propose, write, approve, record a result, or read a run's work | Only `list_topics` and `get_topic` declare the operator kind. `tools/list` shows nothing else, and any other call is refused as an unknown tool. A test walks every registered tool against an operator token | [read_tools.py](../src/jotflow/whiteboard/read_tools.py), [server.py](../src/jotflow/whiteboard/server.py), [test_operator.py](../tests/test_operator.py) |
| Read another channel | Reads are limited to the token's channels. A topic elsewhere is reported exactly like a missing one | [read_tools.py](../src/jotflow/whiteboard/read_tools.py) `_channel_for` |
| Keep using a leaked or unwanted token | `operator revoke` stops it within 2 seconds. Every token expires, after 30 days by default and 365 at most. `operator list` shows when each was last used | [operator_tokens.py](../src/jotflow/whiteboard/operator_tokens.py), [commands/operator.py](../src/jotflow/commands/operator.py) |
| Load the database with guessed tokens | A token the server doesn't know triggers at most one reload a second. Run tokens never do | [operator_tokens.py](../src/jotflow/whiteboard/operator_tokens.py) `check_on_miss` |
| Read without a trace | Each call is logged with the token id, tool, channel and time, never topic content | [read_tools.py](../src/jotflow/whiteboard/read_tools.py) `operator_audit` |
| Reach the server from the network | No port is published. The bridge runs in JotFlow's container, so using it needs `docker compose exec` on the host. The token is passed in the environment, never on a command line | [commands/operator.py](../src/jotflow/commands/operator.py) |

### Someone on the host or the internal network

| Threat | Defence | Enforced in |
|---|---|---|
| Reach the MCP server or a runner | No ports are published. The MCP gate refuses any Origin, a Host off the allowlist, query strings, unpinned protocol versions, bodies over 64 KiB, and missing or invalid tokens. Runners refuse a missing or wrong secret, compared in constant time | [gate.py](../src/jotflow/whiteboard/gate.py), [runner.py](../src/jotflow/adapters/runner.py) `RunnerGate` |
| Read the database | The file is created 0600, with `secure_delete`. It holds pointers, ids and drafts, never thread text, and drafts are cleared once final | [store/sqlite.py](../src/jotflow/store/sqlite.py), [test_store_schema.py](../tests/test_store_schema.py) |
| Catch a runner secret or run token by answering a service name, for example through a resolver that answers every unknown name with a public address (#234) | No service has a DNS search domain. JotFlow resolves each runner's name once, refuses unless every address is private, loopback or link-local (100.64/10 included, for VPNs), and connects to the checked address. A runner refuses a run unless JotFlow's MCP host resolves the same way. Public addresses need an explicit opt-in, `JOTFLOW_RUNNER_ALLOW_PUBLIC` or `JOTFLOW_MCP_ALLOW_PUBLIC` | [netcheck.py](../src/jotflow/netcheck.py), [launch.py](../src/jotflow/orchestrator/launch.py), [runner.py](../src/jotflow/adapters/runner.py), [docker-compose.yml](../docker/docker-compose.yml) |
| Read secrets in logs | Every line passes through the redacting formatter. Message bodies are off by default, and `--log-bodies` is refused in CI and in the image | [logs.py](../src/jotflow/logs.py) |

## Data JotFlow keeps

| Kept | Never kept |
|---|---|
| Message pointers (channel, ts, thread ts), topic ids and titles, chatter marks | Message text, which is fetched live per run and discarded |
| Drafts, until applied, rejected, superseded, or discarded after failing, then overwritten | Tokens, in any form other than a digest |
| Approver, approval time, content hash, result URL | Agent output, which the runner discards |
| Run records and tool-call ledger: ids, states, hashes | GitHub credentials, in JotFlow's own service |
| Identity links: Slack id, GitHub login, time | Display names, beyond a process-lifetime cache |
| Pending debounce times, and the heartbeat file's timestamp | |
| Operator token names, channels, expiry, revocation and last use | |

## Accepted in v1

Each gap is deliberate, visible to the people it affects, and tracked.

| Gap | Why it's accepted | Mitigation today | Follow-up |
|---|---|---|---|
| **Identity squatting:** someone can link another person's GitHub login first | Links are self-declared until verification exists | Cards mark every self-declared login "(unverified)", a login belongs to one person, and assignment needs a participant's link. An approver, an identity admin, or whoever runs JotFlow can remove a squatted link; the person is told and the removal is recorded (#173) | #195: verification |
| **Names in free text under `issue_links = "none"`** | Names can't be detected mechanically | The drafting instructions forbid them, links are refused, and a person reads the full body before approving | None planned. Revisit if a mechanical check becomes practical (#168) |
| **The Codex preset keeps MCP resource tools, for drafting and question runs only** | Codex keeps MCP resource tools that can read any repository text, and its tool set depends on the model (most current models add a code-mode `exec` tool). Setup offers it on equal terms with claude-code, by the maintainer's decision of 2026-10-19 | It refuses execution runs, so an agent holding the write token never has resource tools, and setup keeps `agent-exec.env` on claude-code. It's pinned to Codex 0.163.0 and `gpt-5.5`, and fails closed at runner startup on any other version, flag, or model. The CI gate shows, per kind it serves, no shell or exec tool and a refused `apply_patch`. No real-model pass is recorded yet | #172. Lifting either limit needs a real model run, and a runtime tool-set check or pinned model metadata |
| **The agent CLI resolves JotFlow's MCP host itself** | The CLI makes its own connection, after the runner's check | The runner checks the host before each launch, and with no search domain a stopped `jotflow` fails to resolve rather than pointing elsewhere. Run tokens live only in JotFlow's memory, so one sent while JotFlow is down is already dead. A change of the answer between the runner's check and the CLI's lookup is the gap left | None planned. Closing it would mean handing the CLI a pinned address, which the MCP server's Host allowlist and the presets don't support today |
| **Runs of the same kind can read each other's environment and run directory** | One uid per service, and Linux allows same-uid `/proc` reads | They hold the same token. The other kind's token never exists in that container, and the Claude preset has no built-in tools | #181, following #54 |
| **Open approval mode by default:** anyone in the channel, including the poster, may approve | Many teams are small groups of equals | It's documented plainly. Turn approvers on for wide or external membership. JotFlow warns when `auto` is set in such a channel | D7 |
| **A team's custom agent command** | JotFlow can't vet an arbitrary CLI | The shipped presets are locked down. A custom command keeps the environment allowlist and the 0600 config, but its tools are the team's responsibility. [agent-adapters.md](agent-adapters.md) says what a preset must guarantee and how to prove it | [presets.md](presets.md), [agent-adapters.md](agent-adapters.md) |

## Traceability to architecture.md's Security posture

| Posture bullet | Covered above |
|---|---|
| Slack content is untrusted, and reaches only drafting runs | A channel member; a prompt-injected drafting agent |
| The agent runs in its own container with an allowlist | A prompt-injected drafting agent: "Read Slack or JotFlow secrets" |
| Proposals are validated, and a reported URL must match | A channel member: "Get a write outside the team's repos"; a misbehaving execution agent |
| `ask_channel` posts only in-thread, strips broadcasts, and is rate-limited | A channel member: "Ping the whole channel" |
| MCP tokens are scoped, and Host and Origin are validated | A prompt-injected drafting agent: "Act beyond its run"; a person with an operator token; someone on the host network |
| Issues are written under the agent's identity, naming the proposer | A misbehaving execution agent; the footer rules |
| No secrets in the repo, and JotFlow holds no GitHub credentials | Assets; data JotFlow keeps |
| CI runs forks read-only, never with `pull_request_target` | A compromised dependency or image |
