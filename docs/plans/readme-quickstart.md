# README quickstart

## Links and status

- Issue: #10
- Draft PR: #71
- Status: implementing
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
- [ ] CodeRabbit review, once, if the allowance has reset
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- Every prompt, message, and log line the README quotes was checked against the code on main: init's prompts and closing lines in `commands/init.py`, the token errors in `slack_auth.py`, `jotflow run`'s configuration errors in `commands/run.py`, and the startup and event log lines in `orchestrator/slack.py` and `orchestrator/ingest.py`. The transcript was produced by running init against the fake Slack client.
- Corrections made during that check: the transcript gained init's `COMPOSE_FILE` line, the log lines gained their timestamp prefix, `/invite` uses the bot's handle rather than its display name, and the file-not-found message uses the code's backticks.
- `make check` green; the change is docs only.
- Security review of #71: 4 items, all taken. A troubleshooting row for the directory Docker Desktop creates when `jotflow.toml` is missing; a note that `.env`'s owner-only mode is not enforced on Windows drives, with the advice to clone into the Linux home directory in WSL; a reminder to remove channel ids and workspace names before pasting logs publicly; and a pointer to revoking leaked tokens.

## Handoff notes

None yet.
