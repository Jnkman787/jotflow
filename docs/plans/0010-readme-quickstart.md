# README quickstart

## Links and status

- Issue: #10
- Draft PR: #71
- Status: completed
- Last verified: README written and every quoted string checked against main on 2026-10-18
- Next safe action: follow the README on a fresh clone in the sandbox workspace for the live criterion on #10

## Objective and done-when

A team that has never seen the repo can follow the README alone from clone to a connected bot: `jotflow init`, creating and installing the Slack app, inviting the bot, `docker compose up -d`, and seeing a redacted event in the logs.

Done when:

- [x] Prerequisites: Git, Docker with Compose, and uv (which brings Python 3.12 or newer)
- [x] Clone, `uv run jotflow init`, and what each part of init asks, with a short made-up transcript
- [x] Creating and installing the Slack app from the link init prints, and the two tokens
- [x] `/invite` the bot to each configured channel
- [x] `docker compose up -d`, and how to see it working: a redacted `message channel=... ts=...` log line
- [x] Troubleshooting for the common `jotflow init` and `jotflow run` errors, quoting their real messages
- [x] The status note, "Using it", and "What a team needs" match what works today
- [x] Lists exactly what a team needs (a Slack app) and does not need (GitHub access for JotFlow)

Stays open on #10, because it needs the live sandbox workspace:

- A reader who has never seen the repo reaches a connected bot using only the README

**Out of scope:** the agent container and presets (M3), a full deployment guide (#40). Screenshots: the Slack UI changes often, so the steps name the settings pages instead.

## Design

- One new **Quickstart** section after "How it works", with numbered steps, and a **Troubleshooting** table after it. Everything quoted comes from the code on main: init's prompts, `jotflow run`'s log lines, and its error messages.
- The status note changes from "design phase" to what milestone 1 delivers: JotFlow connects and logs events; drafting issues arrives in later milestones.
- "Using it" gets a line saying it describes the finished product. "What a team needs" lists the real scopes and the machine requirements.
- Made-up names only: `example-org`, `C0EXAMPLE1`, `Idea Bot`, `Example Team`.

## Risks and open questions

- Compose joins the files in `COMPOSE_FILE` with `:` on Linux and macOS but `;` on Windows. The README covers this in Troubleshooting.
- Some workspaces require an admin to approve new apps. The README says so instead of trying to work around it.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Quickstart section
- [x] Troubleshooting table
- [x] Status note, "Using it", and "What a team needs" brought in line
- [x] Check every quoted message and command against the code
- [x] CodeRabbit review: skipped, see Validation
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

- Every prompt, message, and log line the README quotes was checked against the code on main: init's prompts and closing lines in `commands/init.py`, the token errors in `slack_auth.py`, `jotflow run`'s configuration errors in `commands/run.py`, and the startup and event log lines in `orchestrator/slack.py` and `orchestrator/ingest.py`. The transcript was produced by running init against the fake Slack client.
- Corrections made during that check: the transcript gained init's `COMPOSE_FILE` line, the log lines gained their timestamp prefix, `/invite` uses the bot's handle rather than its display name, and the file-not-found message uses the code's backticks.
- `make check` green; the change is docs only.
- CodeRabbit: skipped. The first run hit the free-allowance rate limit; under the maintainer's rule not to wait for it, the retry was stopped.
- Security review of #71: 4 items, all taken. A troubleshooting row for the directory Docker Desktop creates when `jotflow.toml` is missing; a note that `.env`'s owner-only mode is not enforced on Windows drives, with the advice to clone into the Linux home directory in WSL; a reminder to remove channel ids and workspace names before pasting logs publicly; and a pointer to revoking leaked tokens.
- Team lead review of #71: the status note no longer claims a working Slack connection, since the live check has not happened; the recovery row now stops Compose first and starts it again after `jotflow init`.

## Handoff notes

2026-10-19, after the architect's fresh-user pass on main at be3af34 (Refs #41):

- Step 3's list and its sample session now show the visibility question setup asks when `gh` can't look. It comes straight after the repo, so README-order answers had landed in the wrong fields.
- The sample session was re-captured by running init against the fake Slack client, with `claude-code`. It now has the model question, the agent-file lines, the Compose files, and the closing "Next:" line.
- A new step 5, between inviting the bot and starting JotFlow, runs `jotflow check` and points to the optional live check. That matches setup's new closing list from #198, and the sample output and closing lines are the architect's exact wording from #198. The old steps 5 and 6 are now 6 and 7.
- Leftovers: step 7 no longer says the bot posts nothing, and the write-side check no longer says approval in Slack doesn't exist.

2026-10-19, #41 fresh-eyes pass 1, on main at c063833:

- **The blocker.** The prerequisites and "What a team needs" now name the model login and the two GitHub tokens. The agent containers section gives GitHub's click-path for both fine-grained tokens, the two model logins (an API key, or a Claude account login in each container), and a numbered procedure to turn the agent on.
- **The token permissions follow presets.md and agent.env.example:** Contents and Issues read for the read token, Issues read and write for the write token, and Metadata added by GitHub. No read tool in presets.py reads repository contents, so Contents read may be unneeded. That's flagged to security rather than changed here.
- **Consistency.** "Your JotFlow folder" and `uvx jotflow init` are used throughout. Checkout-only commands are marked for contributors. The build and BuildKit text moved to CONTRIBUTING, the `:latest` suggestion is gone, and the manifest's Messages tab row is corrected. Step 2 points servers to /srv/jotflow.
- **MINORs 8 to 15,** as the lead listed them.
