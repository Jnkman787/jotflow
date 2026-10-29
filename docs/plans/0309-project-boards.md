# Add issues to GitHub Projects boards

## Links and status

- Issue: #309
- Draft PR: #311
- Status: planning only, for the lead and security before any code. Decisions 1 to 4 below need them
- Last verified: origin/main b3afbf3, 2026-10-29, with github-mcp-server 2.0.2's projects tools listed offline from the pinned binary
- Next safe action: the lead and security decide 1 to 4

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

The agent writes the issue as today and reports it with `record_result`. JotFlow then asks the execution runner to add it to each approved board, with `POST /boards/add` and a strict JSON body: `{"repo": "owner/name", "issue_number": N, "board": "owner/N"}`. The runner refuses a query string, as RunnerGate does, and a body that isn't exactly that.

The runner makes three fixed GraphQL calls with the write token, each validated:

- the issue's node id, from `repository(owner, name).issue(number)`;
- the board's node id, from `organization(login).projectV2(number)` or `user(login).projectV2(number)`;
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
match = ["Team *"]             # globs on the board's title, ignoring case
exclude = ["* (archive)"]
description = "Each team's own board"
```

- **Validation:** `board` is checked like a repo's owner plus a positive number. Nicknames are unique across a channel's repos, boards and aliases. There are at most 10 boards and 3 board scopes a channel.
- **Discovery:** discovered boards come from the drafting runner, with `GET /boards/{owner}`, a fixed paged GraphQL call with the read token. It returns number, title, short description, closed and template only. Closed and template boards are left out. The catalog, caching, 24-hour staleness and refresh on a miss are as #236's repo catalog, which gains a board half.
- **Untrusted text:** a discovered board's title and short description are GitHub text, cleaned and marked untrusted, as repo descriptions are.

### 3. Propose, card, edit

- **`propose_issue`** gains `boards`, a list of `{board, reason}`, at most 3. Each board must be in the allowlist, or the call is refused with a fixed reason. `reason` is at most 200 characters, cleaned like a question.
- **The card** shows a "Boards" line, with each board's nickname and title, and the agent's reason under it as the agent's words. A pending card gets a multi-select of the channel's boards, using Slack's `multi_static_select`, up to 100 options. Approvers and participants may edit it, as the repo dropdown allows.
- **The edit is a card edit.** `proposal_edits.field` gains `boards`. Its CHECK constraint means a table rebuild in a migration, the one after #304's, so 17 if #304 takes 16.
- **Approval** locks `boards` with the rest of the draft, in its hash, so a person approves exactly the boards the card shows.

### 4. Drafting

drafting.md gains a short rule. Propose boards only from `channel.boards`, by their descriptions, and by the team's triage guide, which is `conventions`, or a doc in `read_paths`. Give a one-line reason, and propose none when none fits. The packet gains `channel.boards`: nickname, owner/number, description, and for discovered ones the untrusted GitHub title and description.

### 5. The permission, and checking it

- **Classic tokens** need the `project` scope.
- **Fine-grained tokens:** the write token needs **Projects: Read and write** on the organisation, an organisation permission that reaches every board of the org. The read token needs **Projects: Read-only** for discovery. Whether fine-grained tokens can reach user-owned boards at all is to be checked (decision 4).
- **Checking:** `jotflow check` can't reach GitHub, so the execution runner gains `GET /boards/{owner}/{number}/access`, a boolean, as #236's repo access check. JotFlow asks it at startup and when the allowlist changes. A board the write token can't reach is reported in the log and by `jotflow check` when it can reach the runners, and that board is turned off.

### 6. Threat model

- **The write token's Projects permission reaches every board of the owner.** Only the fixed add call uses it, on allowlisted boards, so a prompt-injected execution agent never has a projects tool.
- **A planted board description** could steer drafting toward a board. That's the same defence as #236: the allowlist, cleaning, untrusted marking, the card, and a person's approval.

## Decisions for the lead and security

1. **A fixed runner call, not `projects_write` for the agent.** The issue says "through GitHub's MCP projects tools". I recommend against that, because the tool can't be narrowed to "add", as above. The fixed call does only the add, on allowlisted boards. The alternative is to give the agent `projects_write` and rely on the instructions, which JotFlow can't enforce.
2. **Discovery in the first PR, or configured boards first.** I'd ship configured boards first, then discovery in a second PR on #236's catalog. Each PR stays reviewable, and configured boards alone meet most teams' needs.
3. **The permission check:** a runner access endpoint, checked at startup and reported by `jotflow check` when the runners are reachable. The alternative is documentation only.
4. **User-owned boards:** support them only if a fine-grained token can reach one. If not, the first PR is organisation boards only, and says so. One read-only call with a real token settles it, which needs someone with an account.

## Risks and open questions

- **Two migrations in a row:** #304 and this one. Product confirms the numbers.
- **GraphQL in the runner** is new: #236's calls are REST. The calls are fixed strings with variables, never built from input, and the response is read with a strict model, so the risk is in parsing. Size caps and strict models apply.
- **Rate limits:** one add a board, at most 3 a proposal. JotFlow retries a failed or rate-limited add a few times with backoff, never re-running the agent, since the issue already exists. One that still fails is named in the thread notice, for a person to add by hand.

## Progress checklist

- [x] Read the issue, and list the pinned server's projects tools
- [x] Commit this plan and open the draft PR
- [ ] The lead and security decide 1 to 4
- [ ] Code, in the PRs decision 2 settles

## Validation

The projects tool list comes from github-mcp-server 2.0.2 run offline, in a loopback-only network namespace, with `--toolsets projects` and a dummy token. No GitHub call was made.

## Handoff notes

All examples stay fictional: Acme Co, `C0EXAMPLE1`, `acme-co`.
