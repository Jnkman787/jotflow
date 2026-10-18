# Fix the fresh-eyes review findings in the README and setup prompts

## Links and status

- Issue: #100
- Also serves: #7, #10, #90
- Draft PR: #101
- Status: round 2 ready for review
- Last verified: `make check` green locally on Python 3.12 after rebasing onto #98: 458 tests
- Next safe action: review round 2; then the team lead runs the third fresh-eyes review on the merged text

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

- `make check` passes, with 458 tests after rebasing onto #98. The #90 tests still check that every prompt is explained just before it is asked, and their scripted sessions now answer the new "already created the app?" question. New tests cover:
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
- Security review: approved, with one optional note, taken by team lead decision. If the PowerShell live-check line is stopped with Ctrl-C, `Remove-Item` never runs, so the README says to close the window or clear the variable by hand.
- CodeRabbit: skipped, rate limit. A self-review softened one claim, that Slack always notifies you of an approval, to "usually".

## Handoff notes

- `init.py` constants `STEP_TITLES` and the README's numbered setup list must stay in step; `test_steps_are_numbered_like_the_readme` fails if they drift.
- The preset default is `none` until milestone 3. When presets ship, #22 should change `DEFAULT_PRESET` and the README's setup step 6 together.

## Round 2

The second fresh-eyes review (comment on #100, 2026-10-18) found four platform blockers, six contradictions, minor items, and a jargon list. Round 2 is PR #108, `Refs #100`; the team lead runs a third review on the merged text. #100 closes when a review finds no blocker and no contradiction.

| Finding | Fix |
|---|---|
| Blocker: WSL, `docker` not found | Prerequisites say to switch on Docker Desktop's WSL integration for the distribution; a troubleshooting row names the error. |
| Blocker: Linux Docker Engine, `docker.sock` permission denied | Prerequisites say `sudo systemctl start docker` and `sudo usermod -aG docker $USER`, then log out and in; a troubleshooting row names the error. |
| Blocker: Ctrl-D does not stop setup in PowerShell | Setup and the README say Ctrl-C everywhere; the code already treated Ctrl-C as a clean stop. |
| Blocker: Windows needs `COMPOSE_PATH_SEPARATOR=:` | Setup writes it into `.env` on Windows when it is absent, and says so; an existing value is left alone. The README no longer asks people to edit `.env`. |
| The example file says copy it and sets `preset = "claude-code"` | Its header says setup writes `jotflow.toml` and the example shows every option; the preset is commented out until agent runs exist. |
| The missing-scope error suggests recreating the app | It now says where to add the scope, under Bot Token Scopes, and "Do not create a second app". |
| Admin approval says "nothing is saved" | It says `.env` and `jotflow.toml` are not written yet; only `slack-app-manifest.json` was, and it is fine to leave. |
| manifests/README says `/invite @YourBotName` | It says the bot's handle, such as `@idea_bot`, which setup prints. |
| The re-run question says "the JotFlow app" | It asks "Have you already created a Slack app for JotFlow?", which holds whatever the bot is called. |
| README "program", prompt "CLI" | Both say "program". |
| Minor: uv needs a new terminal; macOS Python is older; `cd ~` in WSL; showing the manifest file; viewing the app-level token again; the channel must exist and browser links work; refuse unsupported presets; where logs go and stopping `jotflow run`; plain live-check steps; explain the proxy | All done. Setup accepts `app.slack.com/client/T.../C...` links, and refuses presets it does not ship yet, with `none` or `claude-code` accepted. A pip row covers too-old Python. |
| Jargon | Defined in "Words used here": manifest, Docker, image, container, Compose, slash command, milestone. Inline: sudo, chmod, daemon, bind source path, proxy, OAuth & Permissions, Event Subscriptions, `.venv`, `ts` and `thread_ts`. Removed from user-facing text: "MCP server hat", "headless mode", "CLI", "policy file", "host", "CI", "shell profile", "opt in", and "decision D2" and the bare `debounce_seconds` comment in the example file. |
| Also, from the reviewer | AGENTS.md and the architecture intro say decisions D1 to D13. |

CodeRabbit: skipped, rate-limited.

Reviewer's nit on #108, taken: on a re-run where `jotflow.toml` already names a preset JotFlow does not ship, setup says so before offering none as the default, instead of dropping it silently.

Declined: none. "Gemini CLI" in the README's diagram stays, as it is the product's name.

Validation, round 2: `make check` green, 753 tests. New tests cover the Windows separator (written, skipped elsewhere, existing value kept), channel links from Copy link and the browser, preset refusal, the reworded reuse question, Ctrl-C in the intro, and the missing-scope advice.

## Round 3

The third fresh-eyes review (2026-10-18) found no blockers and four contradictions, which gate #100, plus minor items and jargon. Round 3 is PR #113, `Refs #100`; the team lead runs a fourth review on the merged text. Every quoted string was checked against the code first. The code check showed that JotFlow posts nothing in Slack in this release: no pinned help message, no `/jotflow` handler, no replies.

| Finding | Fix |
|---|---|
| C1: setup accepts `claude-code` but the README said it "will be" the first preset | README and prompt both say `claude-code` is accepted today but does nothing until milestone 3, and other names are refused. |
| C2: the missing-scope error said only "Reinstall" | The error gives the clicks: OAuth & Permissions, Scopes, Bot Token Scopes, Add an OAuth Scope, Reinstall to Workspace, Allow, then paste the new token. |
| C3: README said run setup again, but setup asks again at once | The README row describes the retry: paste the new token when asked, five tries, and run setup again only if it already stopped. |
| C4: manifests/README said the bot pins a help message | Its permission table marks every permission for a later feature *Later*, with a note that this release only reads. "What works today" says JotFlow posts nothing in Slack yet. |
| 1: status banner | "Setup and `jotflow run` are ready to use." |
| 2: Linux Docker Engine link | Added. |
| 3: Create New App greyed out | Ask a workspace admin to allow app creation. |
| 4: PowerShell clone folder | Clone outside OneDrive and Documents, for example `C:\src`; the Windows-drive warning is expected there. |
| 5: wrapped link opens an error page | Use the fallback. |
| 7: token rotation error gave no clicks | The error and a README row explain rotation, that Slack does not allow turning it off, and the recovery; manifests/README.md too. |
| 8: lowercase channel id | Refused with a hint, never guessed: an input that fits the id pattern only in capitals gets "Slack channel ids use capital letters ... did you mean C...?" if it has a digit, or "looks like the channel's name" if not. Reviewer found the first version uppercased names such as `customer12` into valid-looking ids. |
| 9: what `re: web#42` means | Said at the nickname prompt and in the README. |
| 10: labels clutter | Cut "which JotFlow cannot check itself" and the `labels = []` line from the README step. |
| 12: invite | "If Slack asks whether to add the app to the channel, click Add." |
| 13: COMPOSE_PATH_SEPARATOR note | "On Windows in PowerShell ... In WSL this doesn't apply." |
| 14: see it working | The bot posts nothing back in this release; the log is where you see it. |
| 15: live check with `uv run jotflow run` | Or press Ctrl-C in its terminal. |
| 16: exact commands | `rm -r`, `sudo rm -r`, or `Remove-Item -Recurse` for the directory; `JOTFLOW_MCP_PORT=8766` in `.env` for the port; for the proxy, the same `HTTPS_PROXY` line in `.env` reaches JotFlow in Docker. Checked: the Slack SDK's web and Socket Mode clients both read `HTTPS_PROXY` from the environment, and Compose passes `.env` into the container. |
| Jargon | Defined in "Words used here": `jotflow.toml` and TOML, label, OAuth, handle. Inline: bind source path, root, chmod, port, token rotation, orchestrator. Reworded out: mounts (also in setup's own output), exit code 3, "keys", pipe, "Compose v2.24", correlates. `thread_ts` was already explained beside the log. |

CodeRabbit: skipped, rate-limited.

Declined: none.

Validation, round 3: `make check` green, 854 tests. New tests cover the missing-scope clicks and retry, the expired-token message, lowercase channel ids and names that must still be refused, and the preset and nickname wording.
