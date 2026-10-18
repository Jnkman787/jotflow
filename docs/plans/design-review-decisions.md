# Record design review decisions D6 to D12

## Links and status

- Issue: #51
- Draft PR: #52
- Status: ready for review
- Last verified: product, architect, and security reviews applied
- Next safe action: maintainer review and merge

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
- [x] Update affected issues and create new ones
- [x] Teammate review of the diff
- [ ] Mark the PR ready for review

## Validation

- Product review: five fixes applied (keyword acknowledgement, re: handles in link replies, newcomer tip, init records the agent preset, README Using it section) plus the optional D2 wording.
- Security review: three fixes applied (open-mode risk stated plainly with an auto-mode warning, read access can still leak repo content into Slack, drafting guarantee reworded as preset configuration with two tokens).
- Architect review: six fixes applied (runner in the agent container instead of the Docker socket, internal-network binding, two GitHub tokens chosen per run kind, random marker found by listing rather than search, explicit proposal state transitions and a per-thread lock covering execution, question-run token). Overlapping issue #33 closed as superseded by #56.
- Issues: 23 existing issues annotated, 5 more after the mechanics review, 8 new issues created (#53 to #60) and attached to their epics.

## Handoff notes

Nothing outstanding. Code changes implied by these decisions are tracked in their issues, starting with #5, #6, and #8 in milestone one.
