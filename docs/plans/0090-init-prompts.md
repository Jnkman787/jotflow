# Self-explanatory `jotflow init` prompts

## Links and status

- Issue: #90 (Refs: the live box stays open)
- Also serves: #7, #92 (labels prompt wording only)
- Draft PR: #93
- Status: ready for review
- Last verified: `make check` green locally on 2026-10-18, 310 tests, 96.7% coverage
- Next safe action: rebase on #92 when it merges and confirm an empty labels answer means any existing label there; then the live run for the last box on #90

## Objective and done-when

The first live run of `jotflow init` showed that even someone who knows the project was unsure what several prompts wanted. Every prompt now says what it asks for and why, with a made-up example, and the two token prompts name the exact Slack page and button.

Done when:

- [x] An intro paragraph before the first question says what init will do and which files it writes; `jotflow init --help` shows the same text
- [x] Every prompt has a one-line explanation and a fictional example: bot name, app-level token, bot token, channel id, GitHub repo, nickname, what it covers, labels, agent preset
- [x] The app-level token prompt names Basic Information, App-Level Tokens, Generate Token and Scopes, the `socket-mode` name suggestion, the `connections:write` scope, and the `xapp-` prefix
- [x] The bot token prompt names Install App, Install to Workspace, Allow, Bot User OAuth Token, and the `xoxb-` prefix
- [x] Examples use only fictional names: `acme-co/web`, `#ideas`, `Idea Bot`, `C0IDEAS01`. Tests check that no prompt text names a real organisation
- [x] The README quickstart walkthrough matches the new text
- [x] `make check` passes, including the coverage floor

Stays open on #90, because it needs the live run:

- A person unfamiliar with Slack apps completes init from the prompts alone

**Out of scope:** what labels mean in `jotflow.toml`. That is #92, which the architect is changing in `policy.py`. Here only the labels prompt's wording changes, and an empty answer writes no label list, whatever that means in the policy.

## Design

- Every explanation sits in `src/jotflow/commands/init.py` as module-level text, so the prompts, `--help`, and tests share one source.
- A small `_explain(console, text)` prints an indented explanation line just before a prompt. Prompts stay short, and the default stays in brackets.
- The token steps are reordered to follow Slack's own page order: the app-level token, from Basic Information, comes before installing the app and copying the bot token. Each paste prompt is preceded by its page-and-button line.
- The intro: what init does, in four steps, and the two files it writes, `.env` with the tokens and `jotflow.toml` with no secrets. `add_arguments` sets the subcommand's description to the same text with a raw formatter, so `--help` matches without touching `cli.py`.
- Labels: no default list is suggested any more. An empty answer means no list. The wording follows #92: empty allows any label that already exists on the repo, and a list narrows the choice. If this PR merges before #92, the wording would be ahead of the policy, so it should merge after #92 or together with it.
- Agent preset: the #90 note's wording. The team supplies and controls the agent; JotFlow only needs to know which CLI to start.

## Risks and open questions

- Ordering with #92, as above. I've asked the architect for the field semantics and the PR number.
- Reordering the token prompts changes the transcripts in tests and the README; both are updated here.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Intro and `--help`
- [x] Explanations and examples for every prompt; token page-and-button lines; token order
- [x] Labels prompt wording, aligned with #92
- [x] Tests: every prompt shows its explanation; fictional names only; `--help` text
- [x] README quickstart walkthrough
- [x] CodeRabbit once: 1 finding, fixed
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- `make check` green: 310 tests, coverage 96.7%. New tests in `tests/test_init.py`: the intro comes first; every explanation appears, and each one is the line just before its prompt; both token explanations name the page, button, scope, and prefix; the app-level token is asked before the bot token; `--help` contains the intro; every owner in an example is `acme-co`; `none` is no longer a labels answer; a typed label list is kept.
- The README walkthrough was rewritten from a session run against the fake Slack client, and its old ids (`C0EXAMPLE1`, `example-org`) were replaced with `C0IDEAS01` and `acme-co`.
- Labels: Enter now writes an empty list. On main today an empty list allows no labels; with #92 it allows any existing label, which is what the prompt says. This PR should merge after #92, or the two together.
- CodeRabbit: 1 finding, 1 fixed. The intro said nothing is saved until the last question, but init saves the Slack manifest early so it can be pasted if the link fails. The intro and README now say that only `.env` and `jotflow.toml` wait for the last question.
- Security review of #93: approved. Took the optional note: the intro now says `.env` is readable only by you on Linux and macOS, matching the README's Windows caveat. The other note, the labels overlap with #94, is handled by the agreed merge order.
- Rebased onto main after #94 merged, as one commit, since the six earlier commits each conflicted in the labels prompt. Kept #94's semantics: `labels` is optional, so `any` (or Enter) writes no labels line and an explicit empty list is an error; `none` is explained and asked again. My wording sits on top: the prompt is `Labels [any]` after the two-line explanation. #94's four label tests are kept, adapted to the token order and the shorter prompt.
- #92 is PR #94, which also edits the labels prompt with the same behaviour: blank or `any` is unrestricted, `none` is explained. Agreed order: #94 first, then this PR rebases, keeps #94's `_labels`, and adds this wording.

## Handoff notes

None yet.
