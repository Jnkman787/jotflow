# Allow any existing repo label by default; the label list only narrows

## Links and status

- Issue: #92
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no code yet
- Next safe action: open the draft PR, then make `labels` optional in `policy.py`

## Objective and done-when

Teams no longer have to list labels. By default a proposal may use any label that already exists on the target repo. A per-repo `labels` list in `jotflow.toml` is optional, and when present it narrows the allowed set. A missing or empty list means "any existing label", not "no labels".

JotFlow holds no GitHub credentials, so it cannot check that a label exists. The agent reads the repo's labels with its read-only access and chooses from them (#23), and the execution prompt applies only existing labels (#31). JotFlow checks labels only against a list a team has set (#29).

Done when:

- [ ] A repo entry with no `labels` loads and allows any label; a list narrows; tests cover both
- [ ] Init's labels prompt defaults to unrestricted and explains the choice
- [ ] Architecture doc and example config describe the default
- [x] Notes added to #23, #29, #31 (done by the team lead on 2026-10-18)

**Out of scope:** proposal validation itself (#29), the drafting and execution prompts (#23, #31), and the rest of the init wording, which product is rewording in #90.

## Design

References: D2 and D7 in [roadmap.md](../roadmap.md); Configuration and Approval gate in [architecture.md](../architecture.md).

- **`policy.py`.**
  - `RepoPolicy.labels` stays a tuple that defaults to empty. Empty now means unrestricted, and `restricts_labels` says whether a list is set.
  - `check_labels` returns the requested labels, deduplicated without case, when no list is set. With a list it behaves as today: canonical spelling, or `LabelNotAllowedError`.
  - The error message says "not in {repo}'s label list".
  - Duplicate entries in a list are still an error.
- **`policy_writer.py`.** An unrestricted repo is written without a `labels` line, so the file reads as the default rather than as an empty list.
- **`commands/init.py`.** This is the only file shared with #90, and product was told before it was touched.
  - The labels prompt defaults to `any`. Typing `any`, or leaving it blank, gives an unrestricted repo; a comma-separated list narrows it.
  - `none` used to mean "no labels". It now gets a message explaining the choice, because forbidding all labels is no longer something a policy can say.
  - `DEFAULT_LABELS` goes.
- **`jotflow.example.toml`.** One repo shows a narrowing list and one has none, with a comment explaining the default.
- **Docs.**
  - architecture.md: the Configuration table, the Configuration paragraph, the Approval gate's "what the gate is" paragraph, and the Security posture line say "optional label lists". JotFlow checks labels only when a list is set.
  - roadmap.md: D2, the M1 config bullet, and the M4 validation bullet. The issue says "D7 wording", but the label wording sits in D2's row, so that is the row changed.
  - The README's init transcript shows the new prompt.

Alternative considered: `labels: tuple | None`, with `None` for unrestricted and `()` for none allowed. The decision says empty means any, so a separate "none allowed" state would contradict it.

## Risks and open questions

- An existing `jotflow.toml` with `labels = []` changes meaning from "no labels" to "any existing label". Nothing has shipped yet, so no real deployment is affected. The README and example call it out.
- Product's #90 rewords init prompts in parallel. This change keeps to the labels prompt; any wording overlap is settled with product.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] `policy.py` semantics and `policy_writer.py`
- [ ] Init labels prompt default
- [ ] Add or update tests
- [ ] Update documentation: example config, architecture.md, roadmap.md, README
- [ ] `make check` passes
- [ ] Local CodeRabbit review, or skipped
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
