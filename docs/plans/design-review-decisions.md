# Record design review decisions D6 to D12

## Links and status

- Issue: #51
- Draft PR: #52
- Status: implementing
- Last verified: docs written, not yet reviewed
- Next safe action: update affected issues and create the new ones listed in roadmap.md

## Objective and done-when

Record the outcomes of the three-agent design review and the follow-up discussion with the maintainer, so the plan in the repo matches what was decided and milestone one can start on a correct base.

Done when:

- roadmap.md records D6 to D12 and the milestone changes
- architecture.md reflects every decision and no longer contains overstated or stale claims
- affected issues are updated and new work items exist, attached to their epics
- a teammate review of the PR finds no contradiction with the review reports

**Out of scope:** code changes. The stale docstring in `models.py` and the GitHub App fields in `config.py` are fixed by #6.

## Design

Docs only. Each decision gets a row in the roadmap decisions table with the reason. The architecture doc is rewritten section by section where the decisions land: components, tool table, approval gate, correlation, a new intent keywords section, a new run lifecycle section, data and privacy, permissions, configuration, deployment, security posture. Issue changes are applied with the GitHub CLI after the docs are written, so the docs are the reference the issue text points to.

## Risks and open questions

- The issue sweep touches many issues at once. Each edit appends a dated note instead of rewriting bodies, so the original intent stays readable.
- None open. All seven decisions were confirmed by the maintainer.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] roadmap.md: decisions D6 to D12, milestone changes, post-v1 additions
- [x] architecture.md: apply every decision, fix stale and overstated claims
- [x] SECURITY.md: replace the GitHub writer reference
- [x] README, AGENTS.md, CONTRIBUTING.md: reword the GitHub credentials principle
- [ ] Update affected issues and create new ones
- [ ] Teammate review of the diff
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
