# Fix the fresh-eyes review findings in the README and setup prompts

## Links and status

- Issue: #100
- Also serves: #7, #10, #90
- Draft PR: #101
- Status: completed
- Last verified: `make check` green locally on Python 3.12: 438 tests, 97.43% coverage
- Next safe action: review and merge #101; then the team lead runs the second fresh-eyes review on the merged text

## Objective and done-when

A newcomer who is not an engineer can set up JotFlow from the README and the setup prompts alone. Each of the 25 findings in #100 is fixed, or declined below with a reason. All examples stay fictional.

Done when:

- [x] Every item in #100 is fixed in README.md, manifests/README.md, or the prompt text, or declined in this plan and the PR with a reason
- [x] No example names a real organisation, workspace, channel, or repo
- [x] `make check` passes

The second fresh-eyes review is the team lead's, on the merged text, so the PR says `Refs #100`.

**Out of scope:** new setup features beyond item 7's "already created the app?" question, and any change to what JotFlow does at runtime.

## Design

The steps are numbered 1 to 6 in the README, and setup prints the same numbers, so a reader can follow either one. Product owns init's wording from #90. They were told which prompts this changes before it was touched.

| # | Finding | Fix |
|---|---|---|
| 1 | Token order differs between steps and prompts | Setup already asks for the app-level token before the bot token, matching the README. Two gaps remain. On a re-run it checked the saved bot token before the app token; that order is now matched. Setup also prints "Setup step N of 6" headings that match the README's numbered setup steps. They say "Setup step", not "Step", because the Quickstart has its own numbered sections, 1 to 6, and "Step 3" would be ambiguous. |
| 2 | Bot token location: Install App or OAuth & Permissions | Both pages show it. Install App, where you click **Install to Workspace**, shows the token right after install, so all three texts use that page and mention OAuth & Permissions as the other place it appears. |
| 3 | `slack-app-manifest.json` or `slack-app.json` | Both docs explain the two. Setup writes `slack-app-manifest.json` with your bot name, so use that one. `manifests/slack-app.json` is the checked-in default, named JotFlow, for creating the app without setup. |
| 4 | "Only your user can read that file" on a Windows drive | Setup checks where `.env` lives. On Windows, or under `/mnt/<letter>/` in WSL, it says the file's Windows permissions decide who can read it, and advises a private folder. |
| 5 | Repo prompt shows only owner/name | The prompt becomes `GitHub repo (owner/name or link)`. |
| 6 | Preset default `claude-code` but the README says `none` | The default becomes `none`, because agent runs arrive in milestone 3. The prompt names the first planned preset, `claude-code`. |
| 7 | Admin approval dead end | Setup asks "Have you already created the JotFlow app in Slack?" before printing the create link. Yes skips the link and says to open the existing app at api.slack.com/apps. The README explains **Request to Install**: press Ctrl-D, wait for approval, run setup again, and answer yes. |
| 8 | Troubleshooting says `jotflow init` | It says `uv run jotflow init` throughout, with the no-uv form noted once. |
| 9 | What works today | A bold line above Quickstart says it connects and logs messages, and creates no issues yet. |
| 10 | Prerequisites | Install links for Git and Docker, "start Docker Desktop", and an example of good `docker compose version` output. |
| 11 | WSL advice comes too late | It moves into step 2, before `git clone`. |
| 12 | No-uv path is Unix-only | macOS, Linux, and WSL commands, plus the Windows `py -m venv` and `.venv\Scripts\` variant. |
| 13 | First `uv run` looks frozen | A note that it downloads Python and packages and can take a minute. |
| 14 | Bot name rules | The explanation states them: up to 35 characters, letters, digits, spaces, `.`, `-`, `_`, starting with a letter or digit. |
| 15 | Create link | Sign in to Slack in the browser first. Slack may show **Next** before **Create**. If the long link is cut off, the saved manifest file is the fallback. |
| 16 | App-level token clicks | Any name, **Add Scope**, `connections:write`, **Generate**, then **Copy**. |
| 17 | Hidden paste | Nothing appears as you paste. Right-click to paste in some terminals, then press Enter. Printed before each token, and in the README. |
| 18 | Retries | Each question allows five tries. After that setup stops without changing anything, and you can run it again. Shown in the intro and the README. |
| 19 | `#name` for a channel | A `#name` answer gets a clear message that the id is needed and where to find it. The README shows the channel errors. |
| 20 | Labels must exist | The labels explanation says to list only labels that already exist on the repo. |
| 21 | Docker problems come late | Step 5 warns about "Cannot connect to the Docker daemon" and about the Windows `COMPOSE_FILE` separator. |
| 22 | Live check on other shells | PowerShell and no-uv variants. |
| 23 | Missing-scope fix | The clicks are named: OAuth & Permissions, Scopes, Bot Token Scopes, Add an OAuth Scope, Reinstall to Workspace. Then run setup again with the new token. |
| 24 | Jargon | A short "Words used here" glossary: terminal, the folder to run commands in, Socket Mode, scope, token, `.env`, preset, MCP. |
| 25 | "About five minutes" | Dropped from the README and the setup intro. Both say it usually takes ten to fifteen minutes, longer if Docker builds for the first time or an admin must approve the app. |

None is declined.

## Risks and open questions

- Product's #90 wording is in `init.py` and its tests. This change keeps product's structure and adds to it, and product was told first.
- Making the preset default `none` changes what a newcomer gets by pressing Enter. That matches the README and milestone 1, where no agent runs exist.
- No open decisions.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Init prompts: items 1, 4 to 7, 14, 15, 17 to 20, 25
- [x] README: items 1 to 3, 7 to 13, 15 to 25
- [x] manifests/README.md: items 2, 3, 23
- [x] Add or update tests
- [x] `make check` passes
- [x] Local CodeRabbit review, or skipped
- [x] Mark the PR ready for review

## Validation

- `make check` passes, with 438 tests and total coverage at 97.43%. The #90 tests still check that every prompt is explained just before it is asked, and their scripted sessions now answer the new "already created the app?" question. New tests cover:
  - "Setup step N of 6" headings in order, each matching a numbered step title in the README
  - an existing app skipping the create link and the manifest
  - Request to Install and both token pages being explained
  - the paste hint before each token
  - a `#channel` answer getting the id message
  - the preset defaulting to `none` and naming the first preset
  - the intro stating the tries and no fixed time
  - Windows-drive detection, for `/mnt/c`, `/mnt/d`, a Linux home, and a `/mnt/data` path that is not a drive
  - both forms of the saved-tokens message
- A scripted session was printed and read end to end. The README transcript is a shortened copy of it.
- Fictional names only: the new text names `acme-co`, `Acme Co`, `#ideas`, `C0IDEAS01`, and `Idea Bot`. A scan of the diff found no other organisation, workspace, or repo.
- CodeRabbit: skipped, rate limit. A self-review softened one claim, that Slack always notifies you of an approval, to "usually".

## Handoff notes

- `init.py` constants `STEP_TITLES` and the README's numbered setup list must stay in step; `test_steps_are_numbered_like_the_readme` fails if they drift.
- The preset default is `none` until milestone 3. When presets ship, #22 should change `DEFAULT_PRESET` and the README's setup step 6 together.
