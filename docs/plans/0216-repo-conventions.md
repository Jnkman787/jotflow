# Per-repo conventions and an enforced title pattern

## Links and status

- Issue: #216, rescoped by the lead on 2026-10-20
- Draft PR: #254
- Status: ready for review
- Last verified: origin/main 598a9c5, 2026-10-20
- Next safe action: review by the reviewer and security

## Objective and done-when

#249 has the agent copy each repo's title format and headings from its recent issues. What remains is team-written rules that recent issues can't show, and a title rule JotFlow enforces rather than suggests.

- [x] With neither setting, drafting is unchanged (tested).
- [x] `conventions` reaches the drafting packet, labelled as team policy, capped, and taking precedence over the inferred style.
- [x] `title_pattern` rejects a non-matching proposed title with a fixed reason, and a replay fixture covers the agent's retry.
- [x] examples/jotflow.example.toml and the README document both settings. `jotflow init` keeps both on a rewrite.

**Out of scope:**
- Reading conventions from repo files, which is #209. This needs no new GitHub permission.
- Re-checking a title a person edits on the card. The approver is a person, and the pattern binds the agent.

## Design

**Policy** (`RepoPolicy`, per repo in `[[channels.<id>.repos]]`):
- **`conventions: str | None`.** Plain text of at most 2,000 characters, checked with `unsafe_text_problem` like other policy text, so it has no control characters or bidi overrides. It is the admin's, not Slack's or GitHub's.
- **`title_pattern: str | None`.** At most 200 characters, matched in full against the title, which is at most 256 characters. The title is agent-written, and so shaped by Slack and GitHub text.
  - **The guarantee is a time limit, by the lead's decision of 2026-10-20.** Three review rounds found static backtracking budgets bypassable, first counting repeats, then counting choice points. So every match runs in the third-party `regex` engine with `timeout=0.05` and `concurrent=True`, which releases the GIL, through `asyncio.to_thread`. A match that runs out of time is a refusal, never a match. It counts toward the 3-refusal stop, it's counted per repo, and it's logged with the repo only. `jotflow check` warns about a pattern that runs out of time on a hostile test title.
  - **Engine choice:** `regex` 2026.9.29, uploaded 2026-09-29, has wheels for CPython 3.12 to 3.14 on manylinux x86_64 and aarch64. So does `google-re2`. `regex` was chosen for its explicit `timeout=` and `concurrent=True`. The dev stubs are `types-regex` 2026.9.29.20261005, pinned 15 days old rather than the 3-day-old upload.
  - **A cheap syntax filter** still runs at load:
    - 1 to 200 characters, compiling in both `re`, whose parser the filter walks, and `regex`
    - no lookarounds, backreferences, or conditionals
    - at most 3 repeats that can match more than once
    - no syntax the engines read differently: an unescaped `{` that isn't a numeric repeat, since `regex` reads `{e<=1}` as fuzzy matching, and `[:` or `[[`
  - **The earlier choice-point budget is gone,** as the lead asked: score nothing statically beyond the cheap filter.

**Packet.** `RepoBrief` gains both fields. They're described as "the team's own rules for this repo's issues, from the policy file", in the same policy-not-Slack-text standing as `description` and `labels`. `ChannelBrief`'s docstring already says "Policy, not Slack text".

**Instructions** (drafting.md, step 6). One short paragraph:
- If the repo has `conventions`, follow them for the title and body. They take precedence over the style of recent issues.
- In security's wording, they shape only the title and body's form and content. They never override the rules above: trust, no fabricated facts, a words-only Suggested approach, labels, and every other rule above. The last clause covers #258's @-mention rule when it lands, without an edit.
- If it has `title_pattern`, the title must fully match it, or `propose_issue` refuses.

drafting.md is 7,890 bytes against an 8,000 cap. I'll trim elsewhere first, and the held long-line nit in step 6 gets rewrapped here. If trimming doesn't make enough room, the cap rises to 9,000 bytes, with a note.

**Enforcement.** `propose_issue` refuses a title that doesn't fully match. An update can't change a title, so `propose_update` has nothing to check. The refusal is fixed text with the admin's pattern: "this repo's titles must match title_pattern <pattern>; propose again with a title that matches". A refused call is never stored, so it doesn't reach the proposal cap. A per-run count does instead: after 3 refusals, every further `propose_issue` in that run is refused, with "stop proposing and leave the title to a person".

**Setup and check.**
- `policy_writer` renders both settings when set, using a TOML basic string, multi-line for conventions, so a setup rewrite keeps them.
- `jotflow check` validates them through the policy loader.

**Replay.**
- A scripted call can carry `expect_error`, a substring of the refusal it must get. A call with it that is accepted is itself a failure.
- A new fixture, `title-pattern.json`, has a repo with `title_pattern` and `conventions`. Its script proposes a plain title, gets the refusal, and then proposes a matching one.
- The expectations name only the final proposal, with `title_pattern` in `ExpectedProposal` set to the same pattern.

**Precedence, in one line for the docs:** conventions first, then the repo's recent issues, then the built-in sections.

## Risks and open questions

- **For security:** conventions are admin text, and the agent reads them as instructions. The admin already controls the deployment, so this isn't a new trust boundary. But a copied-in convention could ask for something the drafting rules forbid, such as "include setup commands". The instruction says conventions never override those rules, and the replay invariants still apply.
- **The pattern guard is a syntactic check,** not a proof. Together with the 256-character title, it keeps matching cheap.

## Progress checklist

- [x] Confirm the issue is ready: rescoped by the lead, with no open decision
- [x] Security looks at how conventions text enters the packet: ok as admin policy, with the allowlist for `title_pattern`
- [x] Commit this plan and open the draft PR (#254)
- [x] Policy fields and validators, and the writer
- [x] Packet fields and the drafting.md paragraph
- [x] Enforcement in propose_issue. Updates can't change titles.
- [x] Replay `expect_error` and the title-pattern fixture
- [x] Docs: the example toml, README, usage.md, the threads README, and CHANGELOG
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` passes.
- `jotflow replay title-pattern` passes, both in-process and with `--agent scripted` over real MCP.
- **The validator** accepts real formats, such as `^(feat|fix|docs)(\([a-z-]+\))?: .+$`, `\[[A-Z]+-\d+\] .+` and `(Bug|Feature): .{3,80}`. It refuses each shape security named, `(a+)+`, `(a|a)*`, `(a|ab)*` and `(\w+\s?)*`, plus lookarounds, backreferences, conditionals, and four or more repeats.
- **Slow shapes, timed under `regex` with the 50 ms limit,** against `a`×256, `a`×255+`!` and `ab`×128:
  - security's "three repeats and wide alternations" and the reviewer's "eight ten-way alternations" stop at the limit, 0.05 s, as timeouts
  - security's dot-stars pattern, the reviewer's class-repeat pattern, and both 200-character optional families finish in 4 ms or less, since `regex` handles them better than `re`
- **Tests:**
  - All six slow shapes return within the limit plus overhead, and never match `a`×256.
  - A forced timeout is a timeout, never a match. `.*.*.*!` stays quick, and a conventional title matches.
  - A ticker keeps running during a slow match, so the GIL is released.
  - A seeded fuzz of 60 allowed patterns, with wide alternations and repeated groups, never runs past the limit.
  - In the write tools, a timeout gives the same refusal, counts toward the stop, and logs the repo but never a sentinel title.
- **drafting.md** is now 8,309 bytes, so the test's cap rises to 9,000. The held long-line nit in step 6 is rewrapped.

## Handoff notes

- `title_pattern.py` uses the standard library's private `re._parser`, loaded untyped, so the check sees exactly what `re` compiles. It exists in Python 3.11 and later, and the project needs 3.12 or later.
- #258, refusing un-linked @-mentions, isn't in drafting.md yet. The conventions paragraph's "every other rule here" covers it once it lands.
