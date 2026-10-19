# Split the README into a short front page and three guides

## Links and status

- Issue: #41 (Refs)
- Draft PR: #225
- Status: ready for review, built on main after #226 (d004814)
- Last verified: 2026-10-19, against main
- Next safe action: review

## Objective and done-when

The README is 649 lines. A newcomer has to scroll past the full setup walkthrough, troubleshooting, and day-to-day usage to find out what JotFlow is. The maintainer wants it at about 200 lines, with the detail in `docs/`.

Done when:

- [x] `README.md` is about 200 lines and keeps:
  - the summary and status banner
  - Why
  - How it works, with both diagrams and "The pieces"
  - What a team needs
  - a short Quickstart: the seven steps, one or two lines each, with their commands, linking to the full guide
  - Principles, Roadmap, Contributing, and License
- [x] `docs/install.md` has the full setup walkthrough, from today's Quickstart: Words used here, steps 1 to 7 in full, the admin-approval note, both sample sessions, the agent containers, "Turn the agent on", "Other agents and models", "See it work without Slack", and "Check that the agent can write an issue"
- [x] `docs/troubleshooting.md` has today's Troubleshooting section, including the health check and the live check
- [x] `docs/usage.md` has "Using it", with the generated help block, and "Ask from a terminal (admins)"
- [x] **No fact is lost.** A script lists every block of the old README, normalised for whitespace and heading level, and finds each one in the new README or one of the three guides. The PR lists every moved block and where it went
- [x] **Every link resolves.**
  - The README's in-page anchors (`#the-agent-containers`, `#if-your-workspace-needs-admin-approval`, `#troubleshooting`) point at the new file and anchor.
  - Relative links in the moved text are rewritten for `docs/`, such as `docs/deployment.md` to `deployment.md` and `manifests/README.md` to `../manifests/README.md`.
  - The release notes' "README explains each step" points at `docs/install.md`.
  - A link checker over README.md, docs/, CONTRIBUTING.md, CHANGELOG.md, and manifests/README.md finds no broken relative link or anchor
- [x] **The sync tests follow their blocks.**
  - `test_help.py`'s help-block check reads `docs/usage.md`, since the `jotflow-help` markers move there.
  - `test_init.py`'s "steps are numbered like the README" check reads `docs/install.md`, where the numbered step list moves.
  - The docstrings in `orchestrator/help.py` and `commands/init.py` name the new files.
  - Every README test passes
- [x] `make check` passes

## Design

- **The README stays the front door.** Each Quickstart step keeps its command and one sentence, then links to its section in `docs/install.md`. The sample session, the long explanations, and every edge case move.
- **Moved text moves verbatim.** Only the links and heading levels change, so the fresh-eyes review's wording carries over. Any rewording is a separate follow-up.
- **Anchors in the guides** keep today's heading text where it moves whole, such as "The agent containers" and "If your workspace needs admin approval", so the slugs stay the same in the new file.
- **The generated help block** moves with its markers to `docs/usage.md`. No generator target writes it today: the test compares it with `help_text()`, so the test is what follows it.
- **CONTRIBUTING.md** is unchanged, apart from any link that pointed into a moved README section. None do today.

## Risks and open questions

- **PyPI renders README.md** as the project page, where relative links to `docs/` break. Decided on 2026-10-19 by the lead: the README's links to docs/ become absolute, as `https://github.com/Jnkman787/jotflow/blob/main/docs/...`, so they work on PyPI and GitHub. Links inside docs/ stay relative. docs/releasing.md's "Before the tag" step gets a note that a release may pin those links to its tag.
- **The reorganisation changes paths** that the moved text names, such as `jotflow.example.toml`, `agent.env.example`, and the Compose files. Building after it merges avoids a second pass.
- **Conflicts.** Any README change merged before this one needs re-applying. The build starts from a fresh main.

## Progress checklist

- [x] Map the README's sections, the generated block, the tests that read it, and every inbound link
- [x] Commit this plan and open the draft PR
- [x] Wait for the reorganisation to merge, then rebase
- [x] Move the blocks, rewrite the links, and shorten the Quickstart
- [x] Point the two tests and two docstrings at the new files
- [x] Make the README's docs/ links absolute, and add the pin-to-tag note to docs/releasing.md
- [x] In CONTRIBUTING and docs/install.md's checkout notes, after #226, say: an older checkout's .env may hold `COMPOSE_FILE=docker-compose.yml:docker-compose.policy.yml`; remove that line, then use `make up`. Also use #226's checkout commands, `make up` or `docker compose --project-directory . -f docker/...`, and drop the claim that setup writes COMPOSE_FILE in a checkout
- [x] Run the no-fact-lost script and the link checker, and list every moved block in the PR
- [x] CodeRabbit once, `make check`, then mark ready

## Validation

- `make check` passes.
- The no-fact-lost script reports every old block found.
- The link checker reports no broken relative link or anchor.

## Handoff notes

- **Sizes after the split:** README 157 lines, install.md 391, troubleshooting.md 50, usage.md 68.
- **The no-fact-lost script** checked all 422 blocks of the old README. Two are missing by design: "**Turn the agent on:**" is now a heading, so it can be linked, and the "Using it" heading became usage.md's title.
- **The link checker** found no broken relative link or anchor, in README.md, CONTRIBUTING.md, CHANGELOG.md, manifests/README.md, docs/*.md, and docs/releases/*.md.
- **One fact corrected while moving.** The README's Roadmap line said five milestones, and M6 is now a sixth.
