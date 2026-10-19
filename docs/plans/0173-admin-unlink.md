# Admin unlink for GitHub identity links

## Links and status

- Issue: #173
- Draft PR: #194
- Status: ready for review
- Next safe action: review #194

## Objective and done-when

Links are self-declared (#35), so someone can claim another person's GitHub login first, and the real owner can't take it back without help. This adds a way for a trusted person to remove someone else's link. Verification stays a design note here, per the lead.

- [x] An approver, or someone on a configured admin list, can unlink someone else.
- [x] The person whose link was removed gets a private note.
- [x] The removal is recorded with who, whom and when, and no text.
- [x] Self-unlink and linking keep today's rules: only yourself, and no guests or external users.
- [x] A design for verification is written down below, and an issue is opened for it.

**Out of scope:** verification itself, and any change to how links are created.

## Decisions (confirmed by the lead)

1. **Who may unlink someone else.** The listed approvers, plus an optional `[identity] admins = ["U…"]` list in `jotflow.toml`, for teams that run with approvers off. If both are empty, nobody can do it in Slack, and the reply says to ask whoever runs JotFlow. That person uses the CLI in decision 4. The actor must pass the same check as today: a workspace member, not a guest or external, failing closed. The target can be anyone, guests included, since removing a link only takes away an ability.

2. **How the target is named.** The slash command is set up with `should_escape: false`. So `@alex` arrives as the display name, which can be ambiguous, and changing it means reinstalling the app. Turning escaping on would also make Slack rewrite `/jotflow link @octocat` into a Slack mention whenever a Slack user has that name. So `/jotflow unlink U0ABC123` takes the Slack member ID exactly, never case-folded, which Slack's profile menu copies. The reply shows the person's display name, looked up by ID, so a mistyped ID is obvious. It also accepts `<@U0ABC123>` and `<@U0ABC123|name>`, in case a workspace turns escaping on. A plain `@name` gets a reply saying how to copy the member ID. It's never resolved by name.

3. **The private note to the person.** The app has no DM: no `im:` scopes, and the Messages tab is off. The options:
   - **(a), recommended:** turn on a read-only Messages tab in the manifest (`messages_tab_enabled: true`, `messages_tab_read_only_enabled: true`), and post the note with `chat.postMessage` to the person's user ID. This needs only `chat:write`, which the app has. Existing installs must update the manifest and reinstall. Until then, posting fails, and the actor's reply says the person couldn't be told.
   - (b) an ephemeral message in the channel where the command ran, which only works if the person is in it and vanishes on reload;
   - (c) a one-time note shown the next time the person uses `/jotflow`.

   The lead chose (a), with no fallback. There's no released install before v0.1.0, and the `reaction_removed` manifest update already needs a reinstall, so this change goes with it. If posting fails anyway, the reply asks whoever removed the link to tell the person.

4. **A host CLI for whoever runs JotFlow.** `docker compose exec jotflow jotflow identity unlink U0ABC123` removes a link from the host, records `removed_by = "operator"`, and sends the same note. The issue names "someone who runs JotFlow", who may not be an approver.

5. **The record.** Migration 13 adds `identity_unlinks`: an id, `slack_user_id`, `removed_by` (a Slack id, or `operator`) and `removed_at`. It never holds the login or any text. The log line has the two ids and the action. Self-unlinks are not recorded, as today.

## Design

- `orchestrator/identity.py`:
  - `LinkCommand.handle` routes `unlink <target>` to a new `_unlink_other(actor, target)`;
  - the `admins` and approvers check comes from `Policy`;
  - a `Notifier` protocol posts the note, and a Bolt adapter does it for real.
- The store gains `record_unlink(slack_user_id, removed_by)` and `unlinks()`, in both stores, with contract tests.
- `jotflow identity unlink` lives in `commands/identity.py`.
- The help text and `USAGE` mention `unlink <member-id>` for approvers and admins.

## Verification (design note only)

Goal: a link is marked verified once there is evidence the person controls the login, and the card drops "(unverified)". JotFlow holds no GitHub credentials (D5), so any check runs through the agent container or the installer's own login.

- **Route 1, a public proof:** JotFlow gives the person a one-time code, and the person puts it in their GitHub profile bio or a public gist. A read-only lookup checks for it. It could be the agent's `gh` in a dedicated, tool-limited run kind, or `jotflow init`-style tooling on the installer's login. It needs no new credentials. Its costs: a new run kind or host step, codes that must expire, and a check on GitHub text, which is untrusted.
- **Route 2, GitHub OAuth:** proves control directly. But it needs an OAuth app, a callback URL and a client secret, which means a listening endpoint that JotFlow avoids. It conflicts with the no-inbound design.

Route 1, through a dedicated read-only run, is #195, with no milestone.

## Risks

- An approver could remove links maliciously. Every removal is recorded and the person is told, so it's visible.
- A manifest change needs a reinstall. The fallback in decision 3 covers the gap.

## Progress checklist

- [x] Confirm the decisions
- [x] Store table and contract
- [x] `unlink <target>` with the admin and approver check
- [x] The note, and its fallback
- [x] The host CLI
- [x] Manifest, help and docs
- [x] Tests
- [x] Open the verification issue
