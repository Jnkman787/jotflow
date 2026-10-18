# Allow any existing repo label by default; the label list only narrows

## Links and status

- Issue: #92
- Draft PR: #94
- Status: completed
- Last verified: `make check` green locally on Python 3.12 after the security review: 340 tests, 96.93% coverage
- Next safe action: review and merge #94; #93 (product's init wording) rebases over it, or the other way round

## Objective and done-when

Teams no longer have to list labels. By default a proposal may use any label that already exists on the target repo. A per-repo `labels` list in `jotflow.toml` is optional, and when present it narrows the allowed set. A missing or empty list means "any existing label", not "no labels".

JotFlow holds no GitHub credentials, so it cannot check that a label exists. The agent reads the repo's labels with its read-only access and chooses from them (#23), and the execution prompt applies only existing labels (#31). JotFlow checks labels only against a list a team has set (#29).

Done when:

- [x] A repo entry with no `labels` loads and allows any label; a list narrows; tests cover both
- [x] Init's labels prompt defaults to unrestricted and explains the choice
- [x] Architecture doc and example config describe the default
- [x] Notes added to #23, #29, #31 (done by the team lead on 2026-10-18)

**Out of scope:** proposal validation itself (#29), the drafting and execution prompts (#23, #31), and the rest of the init wording, which product is rewording in #90.

## Design

References: D2 and D7 in [roadmap.md](../roadmap.md); Configuration and Approval gate in [architecture.md](../architecture.md).

- **`policy.py`.**
  - `RepoPolicy.labels` stays a tuple that defaults to empty. Empty now means unrestricted, and `restricts_labels` says whether a list is set.
  - `check_labels` returns the requested labels, deduplicated without case, when no list is set. With a list it behaves as today: canonical spelling, or `LabelNotAllowedError`.
  - The error message says "not in {repo}'s label list".
  - A label name that no repo could have, empty or over 50 characters, raises `InvalidLabelError` either way.
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
- [x] Commit this plan and open the draft PR
- [x] `policy.py` semantics and `policy_writer.py`
- [x] Init labels prompt default
- [x] Add or update tests
- [x] Update documentation: example config, architecture.md, roadmap.md, README
- [x] `make check` passes
- [x] Local CodeRabbit review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make check` passes, with 315 tests and total coverage at 96.78%. The new and changed tests cover:
  - a repo without `labels` allowing any label
  - `labels = []` meaning the same
  - case-insensitive deduplication when unrestricted
  - a list narrowing, with canonical spelling
  - the error naming "label list"
  - impossible label names rejected for both kinds of repo
  - a label listed twice
  - the writer omitting `labels` for an unrestricted repo and writing a list when set
  - init defaulting to `any` and showing `[any]` in the prompt
  - `none` explained, then asked again
  - the answer parsing rules
- The example config now has one repo with a narrowing list and one without, and it still round-trips through the writer.
- CodeRabbit (local CLI, `--committed --base main -c AGENTS.md`): 0 findings.
- Security review, at head 247b432:
  1. Reject an explicit `labels = []`. This contradicts #92's "empty or missing means any", so it went to the team lead to decide. It is not changed yet.
  2. Unsafe label text: taken. One rule, `label_problem`, now applies both to labels in `jotflow.toml` and to labels a proposal sets. It rejects control characters, line breaks, Unicode line and paragraph separators, bidirectional overrides and isolates, and leading or trailing spaces, as well as the length limit. Emoji, including zero-width joiner sequences, stay allowed.
  3. Wording: taken. The docs and example now say the agent is *instructed* to use only existing labels, a prompt rule JotFlow cannot enforce. The architecture doc says the execution run checks each label exists before applying it, because GitHub would otherwise create a missing one.
- The dated notes security asked for were offered to the team lead to post: for #26, escape labels as plain text and mark unrestricted ones; for #31, check that each label exists and fail rather than create one.
- After the review: 340 tests pass, with total coverage at 96.93%.
- Product was told the exact `init.py` changes, for its #93 rewording of init prompts.

## Handoff notes

- #29 should call `RepoPolicy.check_labels` on every proposal. It already returns the labels unchanged, apart from duplicates, when no list is set, so #29 needs no special case.
- #23 and #31 carry the rule that the agent uses only labels that exist on the repo. JotFlow cannot enforce it without GitHub access.
