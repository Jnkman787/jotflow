# Split the README into a short front page and three guides

## Links and status

- Issue: #41 (Refs)
- Draft PR: to be opened
- Status: plan only. The build waits for the architect's root reorganisation, which moves the Docker files to `docker/` and the example configs to `examples/`, so every link points at a final path
- Last verified: 2026-10-19, against main
- Next safe action: review this plan. Build once the reorganisation merges

## Objective and done-when

The README is 649 lines. A newcomer has to scroll past the full setup walkthrough, troubleshooting, and day-to-day usage to find out what JotFlow is. The maintainer wants it at about 200 lines, with the detail in `docs/`.

Done when:

- [ ] `README.md` is about 200 lines and keeps:
  - the summary and status banner
  - Why
  - How it works, with both diagrams and "The pieces"
  - What a team needs
  - a short Quickstart: the seven steps, one or two lines each, with their commands, linking to the full guide
  - Principles, Roadmap, Contributing, and License
- [ ] `docs/install.md` has the full setup walkthrough, from today's Quickstart: Words used here, steps 1 to 7 in full, the admin-approval note, both sample sessions, the agent containers, "Turn the agent on", "Other agents and models", "See it work without Slack", and "Check that the agent can write an issue"
- [ ] `docs/troubleshooting.md` has today's Troubleshooting section, including the health check and the live check
- [ ] `docs/usage.md` has "Using it", with the generated help block, and "Ask from a terminal (admins)"
- [ ] **No fact is lost.** A script lists every block of the old README, normalised for whitespace and heading level, and finds each one in the new README or one of the three guides. The PR lists every moved block and where it went
- [ ] **Every link resolves.**
  - The README's in-page anchors (`#the-agent-containers`, `#if-your-workspace-needs-admin-approval`, `#troubleshooting`) point at the new file and anchor.
  - Relative links in the moved text are rewritten for `docs/`, such as `docs/deployment.md` to `deployment.md` and `manifests/README.md` to `../manifests/README.md`.
  - The release notes' "README explains each step" points at `docs/install.md`.
  - A link checker over README.md, docs/, CONTRIBUTING.md, CHANGELOG.md, and manifests/README.md finds no broken relative link or anchor
- [ ] **The sync tests follow their blocks.**
  - `test_help.py`'s help-block check reads `docs/usage.md`, since the `jotflow-help` markers move there.
  - `test_init.py`'s "steps are numbered like the README" check reads `docs/install.md`, where the numbered step list moves.
  - The docstrings in `orchestrator/help.py` and `commands/init.py` name the new files.
  - Every README test passes
- [ ] `make check` passes

## Design

- **The README stays the front door.** Each Quickstart step keeps its command and one sentence, then links to its section in `docs/install.md`. The sample session, the long explanations, and every edge case move.
- **Moved text moves verbatim.** Only the links and heading levels change, so the fresh-eyes review's wording carries over. Any rewording is a separate follow-up.
- **Anchors in the guides** keep today's heading text where it moves whole, such as "The agent containers" and "If your workspace needs admin approval", so the slugs stay the same in the new file.
- **The generated help block** moves with its markers to `docs/usage.md`. No generator target writes it today: the test compares it with `help_text()`, so the test is what follows it.
- **CONTRIBUTING.md** is unchanged, apart from any link that pointed into a moved README section. None do today.

## Risks and open questions

- **PyPI renders README.md** as the project page, and relative links to `docs/` break there. The README already links `docs/` relatively, so this isn't new. Making the README's links absolute GitHub URLs pinned to `main` would fix it. I'd do that in this PR unless the maintainer prefers otherwise.
- **The reorganisation changes paths** that the moved text names, such as `jotflow.example.toml`, `agent.env.example`, and the Compose files. Building after it merges avoids a second pass.
- **Conflicts.** Any README change merged before this one needs re-applying. The build starts from a fresh main.

## Progress checklist

- [x] Map the README's sections, the generated block, the tests that read it, and every inbound link
- [ ] Commit this plan and open the draft PR
- [ ] Wait for the reorganisation to merge, then rebase
- [ ] Move the blocks, rewrite the links, and shorten the Quickstart
- [ ] Point the two tests and two docstrings at the new files
- [ ] Run the no-fact-lost script and the link checker, and list every moved block in the PR
- [ ] CodeRabbit once, `make check`, then mark ready

## Validation

- `make check` passes.
- The no-fact-lost script reports every old block found.
- The link checker reports no broken relative link or anchor.

## Handoff notes

None yet.
