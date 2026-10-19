# Pick repos from a list in `jotflow init`, and repos from Slack

## Links and status

- Issue: #91
- Also serves: #7 (setup), #122 (visibility from `gh`), D2, D5, D7, D11
- Draft PR: #185
- Status: implementing
- Last verified: 2026-10-19, `make check` passes with the code below
- Next safe action: the architect's call on part 2; CodeRabbit; ready for review

## Objective and done-when

Typing each repo's `owner/name` is error-prone, and slow for a channel that feeds several. With the installer's own `gh` logged in, setup offers a list to pick from instead. The allowlist stays explicit: no `org/*` wildcard, anywhere.

Done when:

- [x] With `gh` logged in, setup lists an owner's repos and lets the person pick several by number. Each picked repo's nickname comes from its name, "what it covers" from its GitHub description, and its visibility from the same listing. All are editable defaults
- [x] Without `gh`, or when it can't list, the typed flow is exactly today's
- [x] JotFlow calls only `gh repo list` and `gh api user`, which both only read. It stores no GitHub credential and prints nothing `gh` says beyond the listing
- [x] No wildcard repo entry is accepted anywhere: setup, the policy loader, and any Slack command
- [x] `/jotflow repos` from Slack, as the default (a) under Risks: it lists privately, and `add` and `remove` say how, with no write. Pending the architect's confirmation
- [x] Tests use fakes for `gh` and Slack, with no network
- [x] `make check` passes

## Design

- **`github_cli.gh_repos(owner)`** runs `gh repo list <owner> --no-archived --limit 200 --json nameWithOwner,description,visibility,isFork`, with the same timeout as `gh_visibility`. It returns `(repo, description, visibility)` tuples, sorted by name, with forks after sources. Anything wrong returns `None`: no `gh`, no login, an unknown owner, a timeout, or bad JSON. Setup then falls back to typing. Only `gh`'s exit status is looked at, and its stderr is never printed, since it can name the account.
- **Which owner.** Setup offers the picker only when `gh api user` names a signed-in login, which also only reads. It then asks for "GitHub organisation or user to list", defaulting to that login. Without a signed-in `gh`, setup goes straight to the typed flow.
- **The picker**, in `_ask_channel_repos`:
  - Setup shows a numbered list: `3  web  (private)  The customer web app`, with descriptions cut to one line.
  - The person types numbers and ranges, such as `1,3,5-7`, or Enter to type repos by hand.
  - For each pick, the existing prompts run with defaults filled in: the nickname from the name, "what it covers" from the description if it passes the 200-character rule, otherwise blank, and the labels. Visibility comes from the listing, so there's no separate visibility question.
  - Repos already in the channel are left out of the list. A list longer than 200 says so, and offers typing for the rest.
- **No wildcards.** `RepoName` already allows only `owner/name`. A test pins that `org/*` and `*` are refused by the policy loader and by setup's repo question.
- **Repos from Slack, part 2.** This is pending the architect, asked on 2026-10-19, because it clashes with today's design:
  - `jotflow.toml` is mounted read-only into the container.
  - D2 makes the policy deploy-time configuration, not runtime state.
  - A new repo also needs the agents' GitHub tokens widened, which only GitHub can do.
  - Default, option (a): `/jotflow repos` lists the channel's repos, privately. `/jotflow repos add` and `remove` reply that the repos live in `jotflow.toml`, to edit it on the host and run `docker compose restart jotflow`, since a bind-mounted file's change alone doesn't make `docker compose up -d` recreate the container. The reply also reminds the person to widen the tokens. Nothing is written, so D7 needs no new rule.
  - If the architect chooses a runtime overlay in the store instead, that becomes a separate PR with its own security review.

## Risks and open questions

- **Part 2's design**, as above.
- `gh repo list` caps at 200 here. A bigger organisation types the rest, and setup says so.
- A repo description is someone else's text. It's only a default, checked by the same 200-character rule, and the person sees it before it's saved.

## Progress checklist

- [x] Confirm the issue is ready: part 1 bounded and testable; part 2 pending the architect
- [x] Commit this plan and open the draft PR
- [x] `github_cli.gh_repos` and `gh_login`, with fakes
- [x] The picker in setup, with the typed fallback unchanged
- [x] The no-wildcard tests
- [x] `/jotflow repos`, per the decision
- [x] Docs: README setup step 5, `jotflow.example.toml` if needed
- [x] CodeRabbit once: four findings, all fixed. The repos reply says to edit jotflow.toml rather than re-run setup, and to restart, not `up -d`. Picking more than 20 repos is asked again rather than cut. The plan's owner default is corrected
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- `make check` passes.
- `tests/test_github_cli.py` covers several things:
  - the listing parsed from `gh`'s JSON, with descriptions on one line, PRIVATE and INTERNAL read as private, forks after sources, and entries without a name dropped
  - the exact read-only `gh repo list` command, with the timeout
  - every failure, including an exit code, bad JSON, a timeout, or no `gh`, meaning "type instead"
  - an owner that isn't a GitHub name never running `gh`, and the login check
- `tests/test_init.py` covers several things:
  - two picked repos, with nicknames, descriptions, and visibility from GitHub, and no visibility question
  - a description too long for a default, which is asked for, and a missing visibility, which is asked
  - Enter at the list going back to typing, with the same answers as before
  - a bad selection asked again, and an owner `gh` can't list falling back to typing
  - wildcards refused by setup and by the policy loader
- `tests/test_help.py` covers `/jotflow repos` listing, an unconfigured channel, `add` and `remove` changing nothing and saying how, and the slash-command routing.

## Handoff notes

None yet.
