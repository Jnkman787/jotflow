# A stated priority becomes a configured label

## Links and status

- Issue: #332
- Draft PR: #351, with #331's plan. It will move to its own PR once built
- Status: planning. The drafting.md wording needs the lead's ok
- Last verified: 2026-10-29, against main
- Next safe action: the lead decides open question 1

## Objective and done-when

"P0" or "urgent" in a post has nowhere to go today. From #332:

- [ ] A channel or repo can map stated priorities, such as P0 or "urgent", to a configured label. The agent applies it, and the card shows it
- [ ] A missing label follows #306's skip-and-warn rule, and JotFlow never creates labels
- [ ] Setting a board's Priority field is noted as a possible extension of #309, not built here
- [ ] Tests cover the mapping, and a replay fixture covers a P0 post

**Out of scope:** a board's Priority field, noted for #309. Inferring a priority nobody stated.

## Design

- **Policy:** a channel gets `priority_labels = { "P0" = "priority: critical", "urgent" = "priority: critical", "P1" = "priority: high" }`, and a repo may set its own, which replaces the channel's for that repo.
  - Keys are 1 to 30 characters of plain text, matched without case, and unique without case.
  - Values are `Label`s. When the repo restricts its labels, each mapped label must be on its list. That's checked when the policy loads, so a typo fails `jotflow check`.
  - The writer round-trips both levels.
- **The packet:** each `RepoBrief` carries that repo's map in force, its own or the channel's.
- **drafting.md, at `labels`:** "If a message states a priority that the repo's `priority_labels` maps, such as P0, add exactly that label. Never infer a priority nobody stated."
- **The write tools:** a mapped label passes the existing label checks unchanged. A proposal carries it in `labels` like any other, so the card shows it, and approval locks it.
- **A missing label:** #347 skips the `[issues]` label only when GitHub definitely says it's missing. Here the same rule covers a mapped priority label.
  - Before a new issue's write, JotFlow asks the runner's `POST /repos/labels/check` about each label in the draft that is a mapped priority label for its repo. A definite no leaves it off.
  - `skipped_label` becomes a newline-separated list of skipped labels, since labels can't contain a line break. The thread notice names each one.
  - The draft's other labels still stop the write when missing, as today.
- **The card:** a mapped priority label shows as "priority: critical (stated priority P0)" only if that fits the card's label line simply. Otherwise it shows as a plain label.
- **Fixture:** `priority-p0.json`: "P0: checkout fails for every card payment" in a channel mapping P0 to `priority: critical`. The proposal's labels include it.

## Risks and open questions

1. **The lead: drafting.md bytes,** about +200, pinned.
2. **The `skipped_label` change** reuses #347's column as a list. A row written by #347 holds one label, which reads as a list of one, so nothing migrates.
3. **Matching "urgent" in ordinary speech** ("not urgent") is the agent's judgment, and the card shows the label before anyone approves it.

## Progress checklist

- [ ] The lead's ok on the wording
- [ ] Policy, the writer, validation and the packet
- [ ] drafting.md, with a pin
- [ ] Execution's skip for mapped labels, and the notice
- [ ] Fixture and unit tests
- [ ] Docs and CHANGELOG
- [ ] `make check`, then mark ready

## Validation

To come.

## Handoff notes

Fictional examples only: `acme-co/web`, `priority: critical`.
