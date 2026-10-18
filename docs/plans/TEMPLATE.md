# <Short work title>

Copy this file to `docs/plans/<short-topic>.md` when you start work on an issue, before writing any code. Delete this paragraph. See the plan-first section of [AGENTS.md](../../AGENTS.md).

## Links and status

- Issue: #<N>
- Draft PR: #<N>
- Status: planning | implementing | blocked | ready for review | handed off | completed
- Last verified: <commit, check, or command and when>
- Next safe action: <the specific next step another agent could take>

## Objective and done-when

<What changes, and the testable criteria that mean it is done. Copy the issue's done-when list and refine it.>

**Out of scope:** <what this work deliberately does not do>

## Design

<The approach. Relevant architecture or roadmap sections and decisions. Files and modules touched. Interfaces added or changed. Alternatives considered and why they lost.>

## Risks and open questions

<Anything that could go wrong, anything not yet decided, and dependencies on other issues.>

## Progress checklist

- [ ] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] <first bounded implementation step>
- [ ] <next step>
- [ ] Add or update tests
- [ ] Update documentation
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

<Commands run and what they showed. Manual checks in the sandbox channel, if any.>

## Handoff notes

<Current state, blockers, and anything the next agent needs to know.>
