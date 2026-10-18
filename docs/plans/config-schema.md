# Settings and jotflow.toml schema

## Links and status

- Issue: #6
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: open the draft PR, then rename `src/jotflow/github/` to `src/jotflow/execution/`

## Objective and done-when

Split configuration into the two tiers of D2, remove every GitHub credential from JotFlow's settings per D5, and bring the shared models in line with D8 and D11.

Done when:

- [ ] `Settings` holds no GitHub credentials and no policy values
- [ ] A Pydantic model for `jotflow.toml` covers each channel's repos with nickname and description (D11), label allowlists, an optional approver list off by default and reaction approval off by default (D7), `issue_links`, the debounce window, and the agent preset name
- [ ] Validation errors name the offending key, for example `channels.C0EXAMPLE1.repos[0].nickname`
- [ ] A loader reads the file with the standard library `tomllib` and reports syntax errors, a missing file, and schema errors in one exception type
- [ ] `jotflow.example.toml` exists with made-up values only, and the test suite loads it
- [ ] `src/jotflow/execution/` replaces `src/jotflow/github/`
- [ ] `models.py` gains a proposal record with id, channel, run link and the full state set, a run model, and a result model for `record_result`; a new-topic proposal needs no pre-existing topic id; the stale docstring is fixed
- [ ] Tests cover a valid file, an unknown key, an unknown channel, a repo outside the channel's allowlist, a label outside a repo's allowlist, and the issue URL check
- [ ] `make check` passes

**Out of scope:** `jotflow init` (#7), proposal validation before a card is posted (#29, which will use the lookups added here), the store (#11), approver enforcement (#34), keyword hints on classifications (M3), and any new dependency.

## Design

References: D2, D5, D7, D8, D11 in [roadmap.md](../roadmap.md); Configuration, Run lifecycle, Channels and repos, and Approval gate in [architecture.md](../architecture.md).

**Settings (`config.py`).** Environment only: Slack bot and app tokens, MCP host and port, database path, and the path to `jotflow.toml` (`JOTFLOW_CONFIG`, default `jotflow.toml`). The GitHub App fields go. `debounce_seconds` moves to the policy file, where architecture.md already places it. `agent_command` goes too: under D6 the runner in the agent container launches the CLI from a preset, so JotFlow only needs the preset name, which is policy.

**Policy (`policy.py`, new).** Pydantic models with `extra="forbid"` so an unknown key is an error:

```toml
issue_links = "permalinks"      # or "none"
debounce_seconds = 120

[agent]
preset = "claude-code"

[approval]
approvers = []                  # Slack user ids; empty means approvers off
reactions = false

[channels.C0EXAMPLE1]
debounce_seconds = 60           # optional override

[[channels.C0EXAMPLE1.repos]]
repo = "example-org/web"
nickname = "web"
description = "The customer-facing web app"
labels = ["bug", "enhancement"]
```

- Channels are keyed by Slack channel id, so error paths name the channel.
- A channel needs at least one repo. Repo names and nicknames are unique within a channel.
- Label allowlists are per repo, because labels are a per-repo concept on GitHub. An empty list allows no labels.
- The approver list and reaction approval are global in v1. Per-channel overrides wait until someone needs them.
- The preset name is a slug. Whether a preset exists is checked by the adapter work in #21 and #22, not here.
- Lookups: `Policy.channel(id)` raises `UnknownChannelError`; `ChannelPolicy.repo(name_or_nickname)` raises `RepoNotAllowedError`; `ChannelPolicy.check_labels(repo, labels)` raises `LabelNotAllowedError`. These are what #29 will call.

**Loader.** `load_policy(path)` reads bytes, parses with `tomllib`, validates, and raises `PolicyError` whose message lists one line per problem in the form `key.path: message`.

**Models (`models.py`).**

- `IssueProposal` and `UpdateProposal` stay the agent-facing content. `IssueProposal.topic_id` becomes optional; absent means a new topic, which JotFlow creates on capture.
- `ProposalRecord` is what JotFlow stores: id, channel id, the drafting run id, state, the proposal, content hash, approver and time, the execution result. The agent never supplies id, channel, or run, so they are not in the tool schema.
- `ProposalState` follows the state diagram in architecture.md: pending, approved, executing, reconciling, applied, rejected, superseded, failed. The #6 notes list seven states; architecture.md adds reconciling, and architecture.md is the contract.
- `Run`, `RunKind` (drafting, execution, question), `RunStatus`.
- `ExecutionResult`: either an issue URL or an error, never both. A helper parses the URL so execution can check it points at the approved repo.

**Rename.** `git mv src/jotflow/github src/jotflow/execution` with a docstring that matches D5.

Alternatives considered: a list of channel tables (`[[channels]] id = ...`) gives error paths like `channels[3]` instead of the channel id. Putting `id`, `channel_id`, and `run_id` on `IssueProposal` would expose them in the MCP tool schema and let the agent claim another channel.

## Risks and open questions

- Removing `agent_command` from `Settings` assumes #21 reads the command from the preset in the agent container. If #21 needs JotFlow to know it, it can come back as policy.
- The `githubkit` dependency is now unused by design. Removing it touches the lock file and is left for a separate change.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Rename `github/` to `execution/`
- [ ] `Settings` without GitHub fields or policy values
- [ ] Policy schema, lookups, and loader
- [ ] `jotflow.example.toml`
- [ ] Models: proposal record, states, run, result, optional topic id, docstring
- [ ] Add or update tests
- [ ] Update documentation: AGENTS.md layout, architecture.md configuration
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
