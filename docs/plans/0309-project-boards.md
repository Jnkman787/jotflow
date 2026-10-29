# Add issues to GitHub Projects boards

## Links and status

- Issue: #309
- Draft PR: #311
- Status: plan approved by the lead and security (2026-10-29), with security's five conditions below. Code is next, configured boards first
- Last verified: origin/main b3afbf3, 2026-10-29, with github-mcp-server 2.0.2's projects tools listed offline from the pinned binary
- Next safe action: PR 1 (#311), the marker check (#320) and the plan fix (#319) are merged, and the startup-check fix with the author check is #323. Next is PR 1b, the card's board edit with migration 19, once product's migrations 17 (#305) and 18 (#308) land. Then PR 2, discovery

## Objective and done-when

Teams track work on GitHub Projects boards. A proposal names zero or more boards, the card shows them, a person can edit them, and after approval the issue is added to each one.

From #309, refined:

- [ ] A board allowlist per channel, each board with a description, configured or discovered.
- [ ] Propose, card and edit support for boards, checked against the allowlist.
- [ ] Execution adds the issue to the approved boards, and fails closed on an unlisted one (tested).
- [ ] The write token's needed permission is documented and checked.
- [ ] Security reviews it.

**Out of scope:**

- Board fields such as Status, Iteration or Priority, which stay empty. A later issue may set defaults.
- Removing an issue from a board, and closed or template boards.
- Updates to an existing issue's boards, since `propose_update` doesn't carry boards.

## What the pinned GitHub server offers

The pinned github-mcp-server 2.0.2's `projects` toolset has three tools, which I listed offline:

| Tool | Read-only | Methods |
|---|---|---|
| `projects_list` | yes | `list_projects`, `list_project_fields`, `list_project_items`, `list_project_status_updates`, `list_project_views` |
| `projects_get` | yes | `get_project`, `get_project_field`, `get_project_item`, `get_project_status_update`, `get_project_view` |
| `projects_write` | no | `add_project_item`, `update_project_item`, `update_project_items`, `delete_project_item`, `create_project_status_update`, `create_project_view`, `update_project_view`, `delete_project_view`, `create_project`, `create_iteration_field` |

**The problem:** a preset allows whole tools, through `enabled_tools`, Claude's allowed tools and the server's own `--tools`. It can't allow one method of a tool. An execution agent given `projects_write` could delete items, edit views or create projects on any board the write token reaches, not just add the approved issue. JotFlow can't see the agent's GitHub calls, so it couldn't check that afterwards either. The token's Projects permission is per owner, not per board: it reaches every board of the org.

## Design

### 1. The add is a fixed call in the execution runner, not an agent tool

The agent writes the issue as today and reports it with `record_result`. JotFlow then asks the execution runner to add it to each approved board, with `POST /boards/add` and a strict JSON body: `{"repo": "owner/name", "issue_number": N, "board": "owner/N", "proposal_id": "<id>"}`. Security's addendum after PR 1 (2026-10-29): the runner adds the issue only if its body carries the proposal's marker line, read in the same issue lookup, and otherwise refuses for good. The runner refuses a query string, as RunnerGate does, and a body that isn't exactly that.

The runner makes three fixed GraphQL calls with the write token, each validated:

- the issue's node id and body, from `repository(owner, name).issue(number)`;
- the board's node id, from `organization(login).projectV2(number)`;
- `addProjectV2ItemById`.

The last call is idempotent: adding an item already on the board returns it, so a retry is harmless. Reading is capped, redirects are refused, and proxies from the environment aren't used, as in #236's calls.

**JotFlow checks before asking:**

- the board is in the channel's allowlist at that moment;
- it was approved, as part of the proposal's locked hash;
- the issue is the one `record_result` recorded, in the approved repo.

A board that fails any check isn't added. The proposal's thread notice names the boards added and any that failed. A failed add doesn't fail the issue, which already exists.

### 2. Policy: boards, configured or discovered

```toml
[[channels.C0EXAMPLE1.boards]]
board = "acme-co/3"            # owner/number, as GitHub shows it in the board's URL
nickname = "roadmap"
description = "Planned work for the next two quarters"

[[channels.C0EXAMPLE1.board_scopes]]
owner = "acme-co"
numbers = [12, 14]             # recommended: these boards, by number
marker = "jotflow:triage"      # or a word in the board's short description...
creators = ["acme-lead"]       # ...counted only on boards these accounts created
exclude = ["* (archive)"]      # title globs that only leave boards out
description = "Each team's own board"
aliases = { design = 12 }
```

- **Validation:** `board` is checked like a repo's owner plus a number from 1 to 10^9. Nicknames are unique across a channel's repos, boards and aliases. There are at most 10 boards and 3 board scopes a channel.
- **Descriptions:** a configured board's `description` is the source of truth. The deploying team's weekly review writes them through a reviewed PR, because GitHub's own descriptions go stale. When discovery comes, a configured description overrides a discovered one for the same board. A discovered board's GitHub description is untrusted text, cleaned and capped, as #236's repo descriptions are.
- **Discovery, in PR 2:** the plan on #309, decided 2026-10-29, is the reference. The drafting runner answers `POST /boards/find` with a strict body: the scope, with creators as account ids. It pages one constant GraphQL query with the read token, over at most 500 boards. It reads number, title, short description, closed, template and creator, and returns only boards in scope. A board is in scope by number, or by the marker on a board a listed creator made. Closed and template boards are left out, and so is any title matching `exclude`. A separate board catalog keeps the listings, with #236's timings: refresh every 15 minutes, unused after 24 hours. It caps found boards at 20 a channel and checks each one's write access once, and only a real no excludes one. The add still checks everything live.
- **Untrusted text:** a discovered board's title and short description are GitHub text, cleaned and marked untrusted, as repo descriptions are.

### 3. Propose, card, edit

- **`propose_issue`** gains `boards`, a list of `{board, reason}`, at most 3. Each board must be in the allowlist, or the call is refused with a fixed reason. `reason` is at most 200 characters, cleaned like a question.
- **The card** shows a "Boards" line, with each board's nickname and title, and the agent's reason under it as the agent's words. A pending card gets a multi-select of the channel's boards, using Slack's `multi_static_select`, up to 100 options. Approvers and participants may edit it, as the repo dropdown allows.
- **The edit is a card edit.** `proposal_edits.field` gains `boards`. Its CHECK constraint means a table rebuild in a migration, which the lead numbered 19.
- **Approval** locks `boards` with the rest of the draft, in its hash, so a person approves exactly the boards the card shows.

### 4. Drafting

drafting.md gains a short rule. Propose boards only from `channel.boards`, by their descriptions, and by the team's triage guide, which is `conventions`, or a doc in `read_paths`. Give a one-line reason, and propose none when none fits. The packet gains `channel.boards`: nickname, owner/number, description, and for discovered ones the untrusted GitHub title and description.

### 5. The permission, and checking it

- **Classic tokens** need the `project` scope.
- **Fine-grained tokens:** the write token needs **Projects: Read and write** on the organisation, an organisation permission that reaches every board of the org. The read token needs **Projects: Read-only** for discovery. Whether fine-grained tokens can reach user-owned boards at all is to be checked (decision 4).
- **Checking:** `jotflow check` can't reach GitHub, so the execution runner gains `GET /boards/{owner}/{number}/access`, a boolean. It asks whether the write token can **update** the board, from the board's `viewerCanUpdate`, not only whether it can see it, as the lead asked. It's a pre-check: the add's own failure handling is the real check. JotFlow asks it at startup and when the allowlist changes. A board the write token can't reach is reported in the log and by `jotflow check` when it can reach the runners, and that board is turned off. Only a real no turns it off; a check GitHub couldn't answer leaves it on (the reviewer's #311 nit, 2026-10-29).

### 6. Threat model

- **The write token's Projects permission reaches every board of the owner.** Only the fixed add call uses it, on allowlisted boards, so a prompt-injected execution agent never has a projects tool.
- **A planted board description** could steer drafting toward a board. That's the same defence as #236: the allowlist, cleaning, untrusted marking, the card, and a person's approval.

## Security's conditions (2026-10-29)

1. **Constant GraphQL.** The three calls are constant query strings, and only typed variables come from input:
   - the owner, checked by `is_owner_name`;
   - the repo name, by `is_bare_repo_name`;
   - the issue and board numbers, as ints from 1 to 10^9.

   A response with a non-empty `errors`, or a null node, is a failure. The add's returned item must have a content id equal to the issue node id looked up. Any other shape is refused.
2. **No projects tool for execution.** The write token now carries an organisation-wide Projects permission, and its GitHub server runs in the exec container. A gate assertion checks that the execution kind's GitHub tool set, as each preset starts the server, has no tool whose name contains "project", for claude-code and copilot. It's pinned in the presets tests.
3. **Discovery, in PR 2.** Any org member can usually create and name an org board, so a title glob lets anyone put a board in scope, which is easier than creating a repo under #236. So:
   - discovered boards show "found by scope" on the card, and the threat model names this;
   - scopes match on board numbers, or on a description marker the team controls, rather than on titles alone;
   - only boards inside the scope reach the packet or Slack. Excluded boards' titles stay in the runner and the catalog.
4. **The read token's Projects: Read** reaches every board's items in the org, including titles of issues from repos no channel feeds. No drafting or question tool reads boards: the GitHub allowlist has no projects tool, and #274's probe still holds. The threat model says so, and the gate asserts there are no project tools for drafting or question, as in condition 2.
5. **The agent's `reason`** is cleaned like a question, capped, and shown as plain_text. Boards are in the approved hash. The thread notice that names failed boards uses JotFlow's nicknames, never GitHub's titles.

## Decisions (approved by the lead, 2026-10-29)

1. **A fixed runner call, not `projects_write` for the agent.** The issue says "through GitHub's MCP projects tools". I recommend against that, because the tool can't be narrowed to "add", as above. The fixed call does only the add, on allowlisted boards. The alternative is to give the agent `projects_write` and rely on the instructions, which JotFlow can't enforce.
2. **Discovery in the first PR, or configured boards first.** I'd ship configured boards first, then discovery in a second PR on #236's catalog. Each PR stays reviewable, and configured boards alone meet most teams' needs.
3. **The permission check:** a runner access endpoint, checked at startup and reported by `jotflow check` when the runners are reachable. The alternative is documentation only.
4. **User-owned boards:** left out. The deploying team's boards are org-owned. The docs say only organisation boards are supported, and the policy refuses a board whose owner is a user.

## Risks and open questions

- **Migrations:** #304 takes 16, #305 takes 17 and #308 takes 18, so this one takes 19 for the `boards` card edit, as the lead set on 2026-10-29.
- **A channel-wide triage guide:** `conventions` is per repo and capped at 2,000 characters. A team's guide to which board a request belongs on may outgrow that, so I opened #317 for a per-channel guide text, policy-trusted like `conventions`, capped at 8,000. Drafting reads it for boards and repos alike.
- **GraphQL in the runner** is new: #236's calls are REST. The calls are fixed strings with variables, never built from input, and the response is read with a strict model, so the risk is in parsing. Size caps and strict models apply.
- **Rate limits:** one add a board, at most 3 a proposal. JotFlow retries a failed or rate-limited add a few times with backoff, never re-running the agent, since the issue already exists. One that still fails is named in the thread notice, for a person to add by hand.

## Progress checklist

- [x] Read the issue, and list the pinned server's projects tools
- [x] Commit this plan and open the draft PR
- [x] The lead and security decide 1 to 4
- [x] PR 1: configured org boards, propose, card display, the fixed add, the access check at start, docs
- [ ] PR 1b: the card's board edit, a multi-select, with migration 19 rebuilding `proposal_edits`' CHECK: #327, after #308's migration 18 landed in #315
- [ ] PR 2: discovery, under security's condition 3 and the decisions on #309. Built on PR 1b's branch, it opens once #327 merges

## Validation

- PR 1: `make check` is green. The GraphQL calls are tested against a fake endpoint, covering errors, null nodes, wrong shapes, a mismatched item, rate limits and size caps. A presets test shows no preset gives any run kind a projects tool.
- Each board add is recorded in the existing tool-call ledger, keyed by proposal and board, so it needs no migration. Only the card edit does.

The projects tool list comes from github-mcp-server 2.0.2 run offline, in a loopback-only network namespace, with `--toolsets projects` and a dummy token. No GitHub call was made.

## Handoff notes

All examples stay fictional: Acme Co, `C0EXAMPLE1`, `acme-co`.
