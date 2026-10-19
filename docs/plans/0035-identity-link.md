# `/jotflow link`: self-service identity linking

## Links and status

- Issue: #35
- Draft PR: pending
- Status: implementing
- Last verified: plan only
- Next safe action: implement, then mark ready

## Objective and done-when

Assignees work only for linked participants (#15), so until people can link themselves, nobody can be assigned. The store already holds links: `link_identity`, `get_identity` and `unlink_identity`, self-declared and stored unverified, with a login claimed by one person. The card already marks such logins "(unverified)". This adds the command.

- [ ] `/jotflow link <github-login>` links the caller's own Slack account. `/jotflow link`, with nothing after it, shows the current link. `/jotflow unlink` removes it. Every reply is private.
- [ ] The login is validated against GitHub's username rule: 1 to 39 characters, alphanumerics or single hyphens, and not starting or ending with a hyphen. A leading `@`, or a pasted `https://github.com/<login>`, is accepted and stripped.
- [ ] Guests and external Slack Connect users are refused, through the shared `SlackPeople` check (#151), which fails closed.
- [ ] Anyone may link only themselves: the command takes no Slack user argument. A login someone else already claimed is refused without naming who.
- [ ] No message text is recorded. The store keeps the Slack id, the login, `verified=false` and the time, and the log keeps the Slack id and the action.
- [ ] Assignees resolve through the link table. #15's `_check_assignees` already does this, and an end-to-end test proves it: link, then propose with that login as an assignee, and the proposal is accepted.
- [ ] Approvers see "unverified": the card's assignee line and people block already say so for every viewer. The link reply says so too.

## Design

`orchestrator/identity.py` adds `LinkCommand(store, people)`. Its `handle(user_id, verb, argument)` returns fixed reply text. Each reply names the login only back to the person who typed it, never anyone else's.

`register_commands` gains `links: LinkCommand | None`, routing `link` and `unlink` the way `mode` is routed (product's function, coordinated). The argument keeps its case. GitHub logins compare case-insensitively, so it's stored as typed.

`/jotflow help` lists `link` and `unlink`.

## Risks and open questions

- **Squatting.** Because links are self-declared, someone could claim another person's login first. The card shows "(unverified)", and the real owner can't take it back without help. v1 accepts this, and verification is #35's later option. An admin path, removing someone else's link, is out of scope, and I'll note it on the issue.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [ ] `LinkCommand`, with tests for link, view, unlink, a bad login, a guest, an external user, a claimed login, and relinking
- [ ] Command routing and help text
- [ ] End-to-end assignee test
- [ ] Docs: README and architecture
- [ ] `make check` passes

## Validation

Pending.
