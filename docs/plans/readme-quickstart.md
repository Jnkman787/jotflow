# README quickstart

## Links and status

- Issue: #10
- Draft PR: to be opened
- Status: implementing
- Last verified: plan written, no README changes yet
- Next safe action: write the Quickstart section in README.md

## Objective and done-when

A team that has never seen the repo can follow the README alone from clone to a connected bot: `jotflow init`, creating and installing the Slack app, inviting the bot, `docker compose up -d`, and seeing a redacted event in the logs.

Done when:

- [ ] Prerequisites: Git, Docker with Compose, and uv (which brings Python 3.12 or newer)
- [ ] Clone, `uv run jotflow init`, and what each part of init asks, with a short made-up transcript
- [ ] Creating and installing the Slack app from the link init prints, and the two tokens
- [ ] `/invite` the bot to each configured channel
- [ ] `docker compose up -d`, and how to see it working: a redacted `message channel=... ts=...` log line
- [ ] Troubleshooting for the common `jotflow init` and `jotflow run` errors, quoting their real messages
- [ ] The status note, "Using it", and "What a team needs" match what works today
- [ ] Lists exactly what a team needs (a Slack app) and does not need (GitHub access for JotFlow)

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
- [ ] Commit this plan and open the draft PR
- [ ] Quickstart section
- [ ] Troubleshooting table
- [ ] Status note, "Using it", and "What a team needs" brought in line
- [ ] Check every quoted message and command against the code
- [ ] CodeRabbit review, once, if the allowance has reset
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
