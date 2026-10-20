# Verify a GitHub identity link with a one-time public code

## Links and status

- Issue: #195, in M6
- Also serves: #35 (self-service links) and #173 (removing a squatted link)
- Status: approved by the lead, 2026-10-20, with three notes folded in below. Implementing.

## Objective and done-when

Links are self-declared (#35), so cards mark every linked login "(unverified)", and someone can claim another person's login first. This lets a person prove they control the GitHub account, by putting a one-time code where only that account can put it. JotFlow holds no GitHub token, and its own process stays off GitHub.

- [ ] A person can ask for a code, publish it on their GitHub account, and have JotFlow check it. On success the link is marked verified.
- [ ] The code is random, short-lived, single-use, bound to one Slack user and one login, and stored only as a digest.
- [ ] The check reaches GitHub without JotFlow holding a GitHub token, and without JotFlow's own container calling GitHub.
- [ ] A verified link drops "(unverified)" from cards.
- [ ] Changing or removing a link drops its verified state.
- [ ] The real owner can verify a login someone else has claimed. The link moves to them, the other person is told, and the move is recorded.
- [ ] The threat model's identity-squatting row narrows.

**Out of scope:** GitHub OAuth, which needs an inbound callback (rejected in #173's design note), and periodic re-verification.

## How the check reaches GitHub

| Option | How | For | Against |
|---|---|---|---|
| **A. JotFlow fetches anonymously** | JotFlow's process calls `api.github.com` with no token. | Simplest. Works without the agent. | It breaks a stated property: the docs and README say JotFlow's container has no GitHub access. It's a new egress destination from the container that holds the Slack tokens, and it parses GitHub's untrusted JSON there. |
| **B. The drafting runner fetches, anonymously** (recommended) | A new secret-gated runner endpoint, `POST /verify`, takes `{login, code}` and reads the user's public profile and gists. It returns only `{verified, github_id, login}`. | JotFlow's container stays off GitHub, so the stated property holds. The agent container already reaches GitHub. No token is needed, because public profiles and gists need none, so the read token isn't used either. | It needs the agent profile on. Linked logins only matter for assignees, which only agent runs use, so a team without the agent has nothing to verify for. It adds runner code outside the agent CLI. |
| **C. An agent run checks it** | A new run kind asks the model to look. | Reuses the run machinery. | It costs model tokens and takes seconds to minutes. It puts an untrusted profile in a model's context to answer a yes-or-no question that plain code answers exactly. Rejected. |

## Decisions (approved)

1. **Option B, anonymous, on the drafting runner.**
   - **The runner side:**
     - It's behind the same gate as `/runs`: the runner secret, the Host allowlist and the size cap.
     - The runner calls only `https://api.github.com`. It uses fixed paths built from a login validated with `check_github_login`, no token, no redirects, `trust_env=False`, a 10 s timeout and a 64 KiB response cap.
     - It runs on the agent's own egress network, as the CLI does.
   - **JotFlow's side:**
     - It calls the runner with the private-address check from #234.
     - It gets back a fixed-shape answer, validated strictly, as `/info` is.
     - The runner never returns or logs the bio or gist text.
2. **Where the code goes:** the account's public profile **bio**, or the **description of a public gist**, whichever the person prefers.
   - The runner reads `GET /users/{login}`. It requires `type == "User"`, so organisations are refused, and takes `id`, `login` and `bio`.
   - If the code isn't there, it reads the 10 most recent gists, `GET /users/{login}/gists?per_page=10`, checking only descriptions.
   - The match is the exact code as a whole word. Nothing else from the text is used.
   - Anonymous API calls allow 60 an hour per IP, shared with anything else on the host. A 403 or 429 from rate limiting is never "not verified". The runner answers `{"rate_limited": true}`, and JotFlow replies with fixed text: "GitHub is limiting requests right now; try `/jotflow verify check` again in a few minutes." The code isn't consumed, and the check doesn't count toward its 5. The docs say heavy use needs patience.
3. **The code:** `jotflow-` plus 12 characters from `secrets.token_urlsafe`, about 72 bits.
   - It's shown once, privately, to the person who asked.
   - It's stored as a SHA-256 digest with the Slack id, the login and an expiry 30 minutes ahead.
   - There's one pending code per person, and a new request replaces the old one.
   - It's single use, and consumed on success.
   - There are at most 5 checks per code, and one check per person every 30 s.
4. **Commands,** all private replies:
   - `/jotflow verify` gives a code for the caller's own linked login.
   - `/jotflow verify your-login` gives a code to claim a login, even one someone else has linked, as long as it isn't verified.
   - `/jotflow verify check` asks JotFlow to check now.

   The reply says where to put the code and that it can be removed once verified. The same guest and external check as linking applies, failing closed.
5. **What verifying does:**
   - **Success marks the link verified,** storing `verified_at`, GitHub's numeric `id` and the canonical login GitHub returns, which fixes the case.
   - **Claiming someone else's login moves the link to the person who proved control.** It applies only when that login is linked, unverified, to another Slack user. The other person is told privately with #173's note. The move is recorded in `identity_unlinks` with `removed_by = "verification"`, with no login and no text.
   - **A login already verified by someone else can be taken over only by a newer proof.** GitHub usernames can be renamed and then claimed by someone else, so the latest proof wins. The previous holder is told and the move is recorded. This is rare, and visible.
6. **Unlinking and relinking:**
   - Self-unlink, admin unlink, and the host CLI delete the link, so its verified state goes with it.
   - Relinking, even to the same login, starts unverified.
   - A pending code is dropped when its link changes.
7. **Cards:**
   - A verified login shows without "(unverified)".
   - An unverified one keeps it.
   - Assignment rules don't change: an assignee is still a linked participant's login.

## Store

Migration 15, since #253 takes 14. Rebase after #253 merges, coordinating with product:
- `identity_links` gains `verified_at` (UTC) and `github_id` (GitHub's numeric user id), both nullable. The `verified` column exists already.
- A new `identity_codes` table holds:
  - `slack_user_id`, the primary key, so one pending code per person;
  - `login`;
  - `code_digest`;
  - `expires_at`;
  - `checks`.
- No code, bio or gist text is stored, and the schema test's comment rules apply.

## Risks

- **A renamed GitHub account.** After a rename, the old login can be taken by someone else, and a verified link to the old login then points at the wrong person. Storing `github_id` lets a later check compare. This plan doesn't re-check on its own, and the threat model records that a verified link is proof at a point in time.
- **The anonymous rate limit** is per IP, shared by everything behind the host's address. The 30 s per-person cooldown and the cap of 5 checks keep JotFlow's share small. Rate-limited answers are retried by the person, not automatically.
- **Untrusted JSON in the runner.** It's parsed with a strict model, capped at 64 KiB. Only three fields are read, and text is never echoed.
- **The code in a public bio** could be seen by others. It's bound to the requester's Slack id and the login, and consumed on use, so seeing it gives nothing.

## Progress checklist

- [ ] The lead approves the decisions
- [ ] The runner's `/verify` endpoint, with tests against a fake GitHub
- [ ] JotFlow's `/jotflow verify` commands, the code store, and the link move
- [ ] Card rendering for verified logins
- [ ] Migration, store contract tests and the upgrade test
- [ ] Docs: architecture's identity section, the threat model's squatting row, the README and the CHANGELOG
- [ ] `make check`
