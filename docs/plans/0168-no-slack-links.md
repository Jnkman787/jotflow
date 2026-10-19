# Refuse Slack links in drafted text when issue_links is none

## Links and status

- Issue: #168
- Status: ready for review
- Next safe action: reviewer reviews

## Objective and done-when

- [x] With `issue_links = "none"`, `propose_issue` refuses a title or body, and `propose_update` refuses a changed body or comment, containing a link to any `slack.com` host or a `slack://` link. The reason is fixed: "This channel keeps Slack out of GitHub (issue_links = none): remove every Slack link from the title, body and comment, and don't name people from the thread."
- [x] The drafting instructions tell the agent not to quote names or links in that mode. `drafting.md` already says so (#144), and `test_instructions` keeps the `issue_links` rule in place.
- [x] Tests cover the refusal, ordinary text that mentions Slack, and the allowed case with `issue_links` on.

## Design

`WriteTools._check_no_slack_links` runs after the repo check and before anything is stored. The pattern `\b(?:[a-z0-9-]+\.)*slack\.com\b|\bslack://`, case-insensitive, catches workspace, `app.`, `files.` and enterprise-grid hosts, and Slack's own `<https://…|label>` markup through its URL. It doesn't match `notslack.com`, `slack.community`, or the word Slack.

Names in free text can't be checked mechanically. That rests on the instructions and the approver, as architecture.md now says.

## Validation

`make check` is green. Tests cover:
- four link forms, each refused in a title and a body;
- an update's body and its comment;
- nothing stored on a refusal;
- ordinary text about Slack accepted;
- links allowed with `issue_links` on;
- over the wire: `isError` with the fixed reason.
