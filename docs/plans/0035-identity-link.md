# `/jotflow link`: self-service identity linking

## Links and status

- Issue: #35
- Draft PR: #166
- Status: ready for review
- Last verified: `make check` green, 2312 tests, on 2026-10-19
- Next safe action: reviewer reviews #166

## Objective and done-when

Assignees work only for linked participants (#15), so until people can link themselves, nobody can be assigned. The store already holds links: `link_identity`, `get_identity` and `unlink_identity`, self-declared and stored unverified, with a login claimed by one person. The card already marks such logins "(unverified)". This adds the command.

- [x] `/jotflow link <github-login>` links the caller's own Slack account. `/jotflow link`, with nothing after it, shows the current link. `/jotflow unlink` removes it. Every reply is private.
- [x] The login is validated against GitHub's username rule: 1 to 39 characters, alphanumerics or single hyphens, and not starting or ending with a hyphen. A leading `@`, or a pasted `https://github.com/<login>`, is accepted and stripped.
- [x] Guests and external Slack Connect users are refused, through the shared `SlackPeople` check (#151), which fails closed.
- [x] Anyone may link only themselves: the command takes no Slack user argument. A login someone else already claimed is refused without naming who.
- [x] No message text is recorded. The store keeps the Slack id, the login, `verified=false` and the time, and the log keeps the Slack id and the action.
- [x] Assignees resolve through the link table. #15's `_check_assignees` already does this, and an end-to-end test proves it: link, then propose with that login as an assignee, and the proposal is accepted.
- [x] Approvers see "unverified": the card's assignee line and people block already say so for every viewer. The link reply says so too.

## Design

`orchestrator/identity.py` adds `LinkCommand(store, people)`. Its `handle(user_id, verb, argument)` returns fixed reply text. Each reply names the login only back to the person who typed it, never anyone else's.

`register_commands` gains `links: LinkCommand | None`, routing `link` and `unlink` the way `mode` is routed (product's function, coordinated). The argument keeps its case. GitHub logins compare case-insensitively, so it's stored as typed.

`/jotflow help` lists `link` and `unlink`.

## Risks and open questions

- **Squatting.** Because links are self-declared, someone could claim another person's login first. The card shows "(unverified)", and the real owner can't take it back without help. v1 accepts this, and verification is #35's later option. An admin path, removing someone else's link, is out of scope, and I'll note it on the issue.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [x] `LinkCommand`, with tests for link, view, unlink, a bad login, a guest, an external user, a claimed login, and relinking
- [x] Command routing and help text
- [x] End-to-end assignee test
- [x] Docs: README and architecture
- [x] `make check` passes

## Validation

`make check` is green, with 2312 tests. `tests/test_identity.py` covers:

- what people type, cleaned: a bare login, `@`, and profile links;
- what isn't a login: leading or trailing hyphens, double hyphens, 40 characters, spaces, mentions, deeper paths, and underscores;
- link, view, unlink, and unlinking twice, with the log holding no login;
- relinking replacing the old login;
- a held login refused without naming its holder;
- a bad login;
- guests and external users refused for link, view and unlink, with their old link untouched;
- a failed lookup refusing;
- routing through `/jotflow`, which acks first, answers privately, keeps case, treats `<@U…>` as not a login, and is case-insensitive for `UNLINK`;
- the fallback hint without links.

`tests/test_write_tools.py` covers the end-to-end path: an unlinked participant can't be assigned, and after `/jotflow link` they can.
