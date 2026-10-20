# Per-repo conventions and an enforced title pattern

## Links and status

- Issue: #216, rescoped by the lead on 2026-10-20
- Draft PR: to be opened with this plan
- Status: planning, waiting on security's look at how conventions text enters the packet
- Last verified: origin/main 598a9c5, 2026-10-20
- Next safe action: once security agrees, add the two fields to `RepoPolicy`

## Objective and done-when

#249 has the agent copy each repo's title format and headings from its recent issues. What remains is team-written rules that recent issues can't show, and a title rule JotFlow enforces rather than suggests.

- [ ] With neither setting, drafting is unchanged (tested).
- [ ] `conventions` reaches the drafting packet, labelled as team policy, capped, and taking precedence over the inferred style.
- [ ] `title_pattern` rejects a non-matching proposed title with a fixed reason, and a replay fixture covers the agent's retry.
- [ ] examples/jotflow.example.toml and the README document both settings. `jotflow init` keeps both on a rewrite.

**Out of scope:**
- Reading conventions from repo files, which is #209. This needs no new GitHub permission.
- Re-checking a title a person edits on the card. The approver is a person, and the pattern binds the agent.

## Design

**Policy** (`RepoPolicy`, per repo in `[[channels.<id>.repos]]`):
- **`conventions: str | None`.** Plain text of at most 2,000 characters, checked with `unsafe_text_problem` like other policy text, so it has no control characters or bidi overrides. It is the admin's, not Slack's or GitHub's.
- **`title_pattern: str | None`.** At most 200 characters, matched with `re.fullmatch` against the title, which is at most 256 characters. Python's `re` has no timeout, so the validator refuses the constructs that backtrack badly:
  - a quantified group that itself contains a quantifier, such as `(a+)+` or `(\w*)*`
  - backreferences and lookarounds

  That leaves what title formats need: alternation, character classes, and simple quantifiers, as in `(feat|fix|docs)\([a-z-]+\): .+`. A refused pattern names the construct.

**Packet.** `RepoBrief` gains both fields. They're described as "the team's own rules for this repo's issues, from the policy file", in the same policy-not-Slack-text standing as `description` and `labels`. `ChannelBrief`'s docstring already says "Policy, not Slack text".

**Instructions** (drafting.md, step 6). One short paragraph:
- If the repo has `conventions`, follow them for the title and body. They take precedence over the style of recent issues.
- They never override the rules above: trust, no fabricated facts, a words-only plan, and labels.
- If it has `title_pattern`, the title must fully match it, or `propose_issue` refuses.

drafting.md is 7,890 bytes against an 8,000 cap. I'll trim elsewhere first, and the held long-line nit in step 6 gets rewrapped here. If trimming doesn't make enough room, the cap rises to 9,000 bytes, with a note.

**Enforcement.** `propose_issue`, and `propose_update` when it changes the title, refuse a title that doesn't fully match. The refusal is fixed text with the admin's pattern: "this repo's titles must match title_pattern <pattern>; propose again with a title that matches". The run's proposal cap still counts refused calls, so a run can't retry forever.

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
- [ ] Security looks at how conventions text enters the packet
- [ ] Commit this plan and open the draft PR
- [ ] Policy fields and validators, and the writer
- [ ] Packet fields and the drafting.md paragraph
- [ ] Enforcement in propose_issue and propose_update
- [ ] Replay `expect_error` and the title-pattern fixture
- [ ] Docs: the example toml, README, and CHANGELOG
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

## Handoff notes
