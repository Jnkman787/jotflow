# Allow any existing repo label by default; the label list only narrows

## Links and status

- Issue: #92
- Draft PR: #94
- Status: completed
- Last verified: `make check` green locally on Python 3.12 after the review changes and rebasing onto #95: 437 tests, 97.39% coverage
- Next safe action: review and merge #94; #93 (product's init wording) rebases over it, or the other way round

## Objective and done-when

Teams no longer have to list labels. By default a proposal may use any label that already exists on the target repo. A per-repo `labels` list in `jotflow.toml` is optional, and when present it narrows the allowed set. A missing list means "any existing label". An explicit empty list is an error, by team lead decision after security review (see Validation).

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
  - `RepoPolicy.labels` is `tuple[Label, ...] | None`, and defaults to `None`. `None` means unrestricted, and `restricts_labels` says whether a list is set. An explicit `labels = []` is an error that says to remove the line, or to list the labels to allow. `None` is a real value, not "the default was skipped", so a dump and reload, as init does, keeps it.
  - `check_labels` returns the requested labels, deduplicated without case, when no list is set. With a list it behaves as today: canonical spelling, or `LabelNotAllowedError`.
  - The error message says "not in {repo}'s label list".
  - A label name that no repo could have, empty or over 50 characters, raises `InvalidLabelError` either way.
  - Duplicate entries in a list are still an error.
- **`policy_writer.py`.** An unrestricted repo is written without a `labels` line, the only way to say "any" in the file.
- **`commands/init.py`.** This is the only file shared with #90, and product was told before it was touched.
  - The labels prompt defaults to `any`. Typing `any`, or leaving it blank, gives an unrestricted repo; a comma-separated list narrows it.
  - `none` used to mean "no labels". It now gets a message explaining the choice, because forbidding all labels is no longer something a policy can say.
  - `DEFAULT_LABELS` goes.
- **`jotflow.example.toml`.** One repo shows a narrowing list and one has none, with a comment explaining the default.
- **Docs.**
  - architecture.md: the Configuration table, the Configuration paragraph, the Approval gate's "what the gate is" paragraph, and the Security posture line say "optional label lists". JotFlow checks labels only when a list is set.
  - roadmap.md: D2, the M1 config bullet, and the M4 validation bullet. The issue says "D7 wording", but the label wording sits in D2's row, so that is the row changed.
  - The README's init transcript shows the new prompt.

Alternatives considered:
- A tuple defaulting to `()`, with empty meaning any. This was the first version. It had to change when an explicit empty list became an error, because a dumped policy re-validates `()` as explicit.
- `()` meaning "none allowed". No decision asks for forbidding all labels.

## Risks and open questions

- An existing `jotflow.toml` with `labels = []` now fails to load with a message naming the key and the fix. Nothing has shipped yet, so no real deployment is affected.
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
  - `labels = []` rejected with the key named and the fix
  - init's `any` writing no labels line
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
  1. Reject an explicit `labels = []`. This contradicted #92's "empty or missing means any", so it went to the team lead, who decided to reject it. It is done: `labels` became optional (`None`) and an empty list is an error.
  2. Unsafe label text: taken. One rule, `label_problem`, now applies both to labels in `jotflow.toml` and to labels a proposal sets. It rejects control characters, line breaks, Unicode line and paragraph separators, bidirectional overrides and isolates, and leading or trailing spaces, as well as the length limit. Emoji, including zero-width joiner sequences, stay allowed.
  3. Wording: taken. The docs and example state "never invent a label" as an explicit rule for the agent, which its instructions carry and JotFlow cannot enforce. The team lead asked for it to be stated as a rule. The architecture doc says the execution run checks each label exists before applying it, because GitHub would otherwise create a missing one.
- Security's follow-up nit, taken by team lead decision: invisible format characters (Unicode category Cf) are rejected, such as U+200B zero-width space, U+FEFF, U+2060, and the soft hyphen, so "bug" and "b\u200bug" cannot look identical on a card. The zero-width joiner U+200D is allowed only between two emoji parts. The part before it may be a symbol, a skin-tone modifier, or the emoji variation selector, so woman technologist, skin-tone variants, and the rainbow flag still pass. A joiner at either end or next to a letter is rejected.
- The team lead posts the dated notes security asked for: for #26, escape labels as plain text and mark unrestricted ones; for #31, check that each label exists and fail rather than create one. The lead also updates #92's text for the empty-list decision.
- After the review changes and the rebase onto #95, the fixture threads: 437 tests pass, with total coverage at 97.39%. #95's fixture test and README were updated for the "label list" wording.
- Product was told the exact `init.py` changes, for its #93 rewording of init prompts.

## Handoff notes

- #29 should call `RepoPolicy.check_labels` on every proposal. It already returns the labels unchanged, apart from duplicates, when no list is set, so #29 needs no special case.
- #23 and #31 carry the rule that the agent uses only labels that exist on the repo. JotFlow cannot enforce it without GitHub access.
