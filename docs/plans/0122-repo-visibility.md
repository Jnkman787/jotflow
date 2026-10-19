# Repo visibility for the public-repo warning

## Links and status

- Issue: #122
- Draft PR: pending
- Status: planning
- Last verified: plan only
- Next safe action: the architect agrees the policy and hash changes, product agrees the card context, then implement

## Objective and done-when

D7 promises a warning on the card when the target repo is public. Nothing supplies that today, so every card says visibility was not checked. Per the lead's decision on #122, the policy file is authoritative, and the agent may only add a warning.

- [ ] The policy schema accepts `visibility = "public" | "private"` per repo and rejects other values. The writer writes it.
- [ ] `jotflow init` fills it from the installer's `gh` login when `gh` is installed and logged in, using `gh api repos/{owner}/{repo} --jq .visibility` with a timeout. Otherwise, or if that fails, init asks. `internal` counts as private. Init never stores or prints a token.
- [ ] `propose_issue` and `propose_update` accept an optional `repo_visibility: "public" | "private"`, recorded with the proposal.
- [ ] The card warns when the policy says public or the agent reports public. A policy "private" and no agent report gives no warning. Neither source gives "not checked". A test shows an agent's "private" can't suppress a policy "public".
- [ ] The README and architecture's D7 text say where visibility comes from.

## Design

**Policy, the architect's file:** `RepoPolicy.visibility: Literal["public", "private"] | None = None`. The writer emits it when it's set, and the example file documents it.

**The agent's report** is `repo_visibility` on `IssueProposal` and `UpdateProposal`. They're stored in `draft_json`, so no migration is needed. It's advisory metadata, not content written to GitHub, so `canonical_json` always leaves it out of the content hash. Approvals made before it existed keep their hash, and the agent can't change the approval lock through it. Execution runs never see it.

**The rule,** in one place: `effective_visibility(policy_value, agent_value)`.
- `public` when either source says public.
- `private` when the policy says private and the agent says nothing or private.
- Otherwise `unknown`, including when only the agent says private, since the agent may only add a warning.

**The card, product's file:** `card_context` fills `CardContext.visibility` for the proposal's repo through that rule. The card's rendering is unchanged.

**A repo change on the card.** If the repo dropdown action later changes a proposal's repo, it must clear `repo_visibility`, because the report was about the old repo. I'll note this on the dropdown's issue, since nothing changes the repo yet.

## Risks and open questions

- **`hashing.py` also changes in #144** (`LATER_FIELDS`). Whichever merges second keeps both: drop unset later fields, and always drop `repo_visibility`.
- **The agent's "private" without a policy value** leaves the card saying "not checked". Only the policy removes that line, which is the stricter reading of "agent may only add".
- **`gh` in init** runs only on the installer's machine, with their own login. JotFlow's runtime still never holds GitHub credentials (D5).

## Progress checklist

- [ ] Agree the policy key with the architect, and the card context with product
- [ ] Policy field, writer, and example
- [ ] Write-tool field, and the hash exclusion
- [ ] `effective_visibility` and `card_context`, with tests including suppression
- [ ] Init's `gh` lookup and its fallback question, with tests and a fake `gh`
- [ ] Docs
- [ ] `make check` passes

## Validation

Pending.

## Handoff notes

Worktree `~/jotflow-work/wt-security-visibility`, branch `feat/repo-visibility`.
