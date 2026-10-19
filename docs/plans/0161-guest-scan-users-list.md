# Check a channel's guests with users.list

## Links and status

- Issue: #161
- Status: ready for review
- Next safe action: review the PR

## Objective and done-when

When someone sets a channel to `auto`, JotFlow warns if it has guests or members from another organisation. The scan called `users.info` once per member, up to 1000. That's a tier 4 method, so a big channel took minutes or hit the rate limit.

- [x] The scan pages `users.list` once, and checks the channel's members by `is_restricted`, `is_ultra_restricted`, `is_stranger` and `team_id`
- [x] A 1000-member channel is checked in a handful of calls, and a test counts them
- [x] A failed or partial scan still warns

**Out of scope:** the person checks for approving, skipping and changing modes. They still use `users.info` for one person at a time.

## Design

`BoltModeSlack.outsiders`:

1. reads `conversations.info` for the shared flags;
2. pages `conversations.members`, up to 1000;
3. pages `users.list` 500 at a time, at most 40 pages, and stops once every member is found.

Each listed member is classified by `people.classify`, which `SlackPeople.person` now uses too, so both paths apply one rule. Without JotFlow's own team, anyone with a team counts as external.

It fails closed:

- A member the directory doesn't list counts as a guest. Slack Connect users from another organisation may not be listed.
- Running out of pages before finding every member adds "more members than JotFlow could check".
- Every call goes through `packet.with_backoff`, with one 20-second budget. A listing that fails, or stays rate-limited past the budget, raises, and `ModeCommand` already turns that into the warning.

**Cost.** For a 1000-member channel in a 4000-person workspace, that's 1 + 5 + 8 calls. `users.list` is tier 2, so a very large workspace is capped at 40 pages and warns instead of waiting.

## Risks

- A workspace over 20,000 people may get a warning it doesn't need, if a member sits past the 40th page. That's the safe direction.
- The scan needs no new scope: `users.list` uses `users:read`, which the app already has.
