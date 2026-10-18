# Settings and jotflow.toml schema

## Links and status

- Issue: #6
- Draft PR: #64
- Status: completed
- Last verified: `make check` green locally on Python 3.12 after merging main, 55 tests
- Next safe action: review and merge PR #64

## Objective and done-when

Split configuration into the two tiers of D2, remove every GitHub credential from JotFlow's settings per D5, and bring the shared models in line with D8 and D11.

Done when:

- [x] `Settings` holds no GitHub credentials and no policy values
- [x] A Pydantic model for `jotflow.toml` covers each channel's repos with nickname and description (D11), label allowlists, an optional approver list off by default and reaction approval off by default (D7), `issue_links`, the debounce window, and the agent preset name
- [x] Validation errors name the offending key, for example `channels.C0EXAMPLE1.repos[0].nickname`
- [x] A loader reads the file with the standard library `tomllib` and reports syntax errors, a missing file, and schema errors in one exception type
- [x] `jotflow.example.toml` exists with made-up values only, and the test suite loads it
- [x] `src/jotflow/execution/` replaces `src/jotflow/github/`
- [x] `models.py` gains a proposal record with id, channel, run link and the full state set, a run model, and a result model for `record_result`; a new-topic proposal needs no pre-existing topic id; the stale docstring is fixed
- [x] Tests cover a valid file, an unknown key, an unknown channel, a repo outside the channel's allowlist, a label outside a repo's allowlist, and the issue URL check
- [x] `make check` passes

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
- Lookups: `Policy.channel(id)` raises `UnknownChannelError`; `Policy.repo(channel_id, name_or_nickname)` raises `RepoNotAllowedError`; `RepoPolicy.check_labels(labels)` raises `LabelNotAllowedError`. All three ignore case, as GitHub does, and share the base `PolicyViolation`. These are what #29 will call.

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
- `ExecutionResult` accepts only `https://github.com/...` issue URLs. GitHub Enterprise Server hosts would need a configurable host later.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Rename `github/` to `execution/`
- [x] `Settings` without GitHub fields or policy values
- [x] Policy schema, lookups, and loader
- [x] `jotflow.example.toml`
- [x] Models: proposal record, states, run, result, optional topic id, docstring
- [x] Add or update tests
- [x] Update documentation: AGENTS.md layout, architecture.md configuration
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check`: ruff lint and format clean, mypy strict clean, all tests pass.
- Tests cover the example file, defaults, unknown top-level and nested keys, a missing key, a bad channel id, invalid values, every problem reported at once, invalid TOML with its line, a missing file, an unknown channel, a repo outside the channel's list, labels inside and outside a repo's allowlist, the new proposal states, a proposal record round trip, the execution-run rule, and issue URL checks.
- CodeRabbit (local, free CLI allowance): 1 finding, 1 fixed. Repo and issue URL patterns used Unicode `\w` and `\d`; they are now ASCII-only, with tests. The same fix was applied to the repo patterns in `policy.py` and `models.py`.
- A sample error from a deliberately broken file:

```
jotflow.toml:
  debounce_seconds: Input should be greater than or equal to 1
  approval.approvers[0]: 'bob' is not a Slack user id such as U0123ABCD
  channels.general: 'general' is not a Slack channel id such as C0123ABCD
  channels.general.repos[0].nickname: 'Web' is not a nickname of lowercase letters, digits, - or _, up to 32 characters
  issue_link: unknown key
```

## Handoff notes

Deviations from the plan, all small:

- Label checks live on `RepoPolicy`, not `ChannelPolicy`, because the allowlist is per repo.
- `jotflow.toml` is added to `.gitignore`. A team's real file names its workspace's channel ids, and only the example belongs in the repository.
- The old model tests used this repository's name and a channel name as sample values; they now use made-up ones.

Follow-ups for other issues: #29 calls the lookups in `policy.py`; #11 stores `ProposalRecord` and `Run`; `githubkit` can be dropped from the dependencies in a separate change.
