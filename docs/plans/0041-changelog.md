# CHANGELOG.md and the v0.1.0 release notes

## Links and status

- Issue: #41 (Refs, since #41 is done only when the maintainer completes the no-clone install)
- Draft PR: to be opened
- Status: draft
- Last verified: 2026-10-19, against main
- Next safe action: review the wording; refresh both files when #185 and any other open feature PR merges

## Objective and done-when

A person deciding whether to run JotFlow, or upgrading it, needs one place that says what each release changed. That place should be written for them, not for contributors.

Done when:

- [x] `CHANGELOG.md` exists in Keep a Changelog form, with an unreleased 0.1.0 section grouped by what a user does: install and setup, Slack, the agent, running it, security, and known limitations
- [x] `docs/releases/v0.1.0.md` drafts the GitHub release's text: what JotFlow is, the two install commands, highlights, what to know before relying on it, and the artifacts
- [x] Every claim is checked against main's code and docs. No internal PR numbers, contributor process, or deployment-specific names
- [x] Known limitations match the threat model's "Accepted in v1" table, plus the store rebuild that isn't built yet
- [ ] On release, the maintainer replaces "unreleased" with the date and pastes the notes into the GitHub release

## Design

- **Source.** All 104 merged PRs since the start, grouped by user-facing effect. Internal changes are left out: CI, tests, refactors, plans, and review tooling.
- **Release links** point at the `v0.1.0` tag, so they keep working after main moves on.
- **Pending work.** The repo picker in setup and `/jotflow repos` come from #185, which is still open. If 0.1.0 is cut without it, delete those two clauses.
- **No Slack app update note.** The README tells older apps to reinstall for `reaction_removed`. Nobody installed a released version before 0.1.0, so the notes leave it out.

## Risks and open questions

- The changelog can drift from main while more PRs merge before the tag. Whoever merges a user-facing PR adds its line under the unreleased section.

## Validation

- `make check` passes, including the line-endings check.
- Every relative link resolves in the repo.
