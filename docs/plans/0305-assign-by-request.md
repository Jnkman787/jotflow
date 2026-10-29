# Assign by request: "add me", an Add me button, and reasons for every assignee

## Links and status

- Issue: #305
- Draft PR: #312, for the plan. Built in #316 (runner checks), #321 and #328 (links), #325 (reasons), and the Add me PR, which brings this plan in and says Fixes #305
- Status: in review. The lead and security approved the plan on 2026-10-29 with the decisions below
- Last verified: 2026-10-29, against main after #325
- Next safe action: review the Add me PR

## Objective and done-when

Teams want people to join an issue themselves. Today an assignee comes only from the agent, for a participant who "said they will take it", or from a person's Make assignee click on a participant's row (#160). Nobody can add themselves to a filed issue without a new agent run and an approval. And a person with no linked login can't be assigned at all until they find `/jotflow link`.

After this:

- Saying "add me" or "I'll help with this" in the thread makes the agent propose that person as an assignee, on the pending draft or as an update to the filed issue.
- An **Add me** button on the card and on the filed-issue reply adds the person who clicks.
- If that person has no linked login, the button opens a Slack modal asking for it. JotFlow checks through the read-only runner that the account exists and can be assigned in the issue's repo, and names any problem.
- The agent may propose zero, one or several assignees, each with a reason shown on the card. It assigns nobody when unsure.
- `jotflow identity link` bulk-loads a team's Slack-to-GitHub map, marked as linked by an admin.

Done when, from #305's boxes:

- [ ] "Add me" in a thread adds a linked person to the pending draft or the filed issue, shown by a replay fixture
- [ ] The button adds a linked person. For an unlinked one it opens a modal that checks the account exists and can be assigned, with named errors, tested with a fake GitHub
- [ ] Assignees on a card carry reasons. Only linked logins are ever assigned
- [ ] `jotflow identity link` bulk-loads links from a file, marked admin-linked
- [ ] Security decides whether self-add may skip approval, and the threat model records the decision
- [ ] `make check` passes

**Out of scope:** removing yourself from a filed issue ("Remove me"), assigning teams, and GitHub's own CODEOWNERS.

## Design

### 1. Reasons on assignees

`IssueProposal` keeps `assignees: list[str]`, which is what's written to GitHub. It gains `assignee_reasons`, one per assignee, in the same order:

```python
class AssigneeReason(BaseModel):
    login: GithubLogin
    why: Literal["volunteered", "named_owner", "owner_hint"]
    source: MessageRef | None  # the message that shows it; None only for owner_hint
```

`UpdateProposal.changes` gets the same for `add_assignees`.

- **The reason is a fixed choice, never agent text.** The card shows JotFlow's own words for each choice, such as "volunteered in the thread", "named as the owner in the thread" or "listed as an owner of web". Where there's a source message, the card adds a link to it. So nothing the agent writes reaches the card through this field, and an injected message can't put words there.
- **`propose_issue` and `propose_update` check every reason:**
  - **volunteered** and **named_owner** need a source message in this run's packet. For **volunteered**, that message's author must be the assignee's linked Slack user.
  - **owner_hint** needs the login to be the linked login of a Slack user in the repo's new `owner_hints`.
  - **Every assignee is a linked login,** as today. The rule that an assignee must be a participant becomes "volunteered, named in a source message, or an owner hint".
- **A person's own add**, from the button or Make assignee, is recorded with `why="volunteered"` or a new `"added_on_card"`, and no source.
- **Old stored drafts** have no reasons. They're read as `assignee_reasons=[]`, and the card shows those assignees without one.

**Owner hints** are a new optional per-repo setting in `jotflow.toml`: `owner_hints = ["U0123ABCD"]`, Slack member IDs. The agent sees them in `channel.repos` with each person's linked login, if any, and may propose one with `why="owner_hint"` when the idea clearly falls to that repo. A hint without a linked login is never assigned.

**drafting.md** changes its assignment rules:
- Propose zero, one or several assignees, each with a reason.
- A volunteer is someone who says they'll take or help with the work, such as "add me", "I'll help with this" or "I can take it". Their own message is the source.
- A named owner is someone the thread names as owning it, such as "Sam owns exports", with that message as the source.
- Use owner hints only when the idea plainly belongs to that repo's area.
- When unsure, assign nobody. An issue with no assignee is normal.
- A mention alone never makes someone an assignee.

### 2. "add me" in the thread

There's no keyword. The reply reaches the agent like any other thread message:
- **On a pending draft:** the drafting run the reply triggers re-proposes the draft with the volunteer added. It supersedes the old card, as any re-draft does today.
- **On a filed issue:** the run proposes an update with `add_assignees` and a `volunteered` reason, and it gets a card and an approval like any agent update.
- **An unlinked volunteer** can't be assigned, so the agent asks nothing and proposes nothing for them. The draft's card shows them under Involved with an **Add me** button, see below, and the run's question budget isn't spent on it.

A replay fixture covers both cases, with a volunteer and an injected "assign @someone-else" line that must not assign anyone.

### 3. The Add me button

**On a pending or failed card,** next to Involved:
- **Who may click:** any member of this workspace, as for Approve, but never a guest or an external user, and never in a paused channel. The click adds only the clicker's own linked login. So, unlike Make assignee, it's allowed with approvers on too. Adding yourself to a draft that still needs approval changes nothing anyone has agreed to.
- **What happens:** it's an edit, through `edit_proposal`, recorded with `field="assign"`. The content hash changes, the card redraws, and an earlier Approve is stale, as for every edit (#160).

**On the filed-issue reply,** the reply becomes blocks: the same text, plus an **Add me** button. Its `text` fallback stays the exact string today's reply posts, which `jotflow rebuild` reads (#38). A click:
- checks the click as above, against the filed proposal's channel and thread
- builds an `UpdateProposal` with `add_assignees=[login]` and an `added_on_card` reason, which no agent wrote
- then either posts a card for approval, or files it at once, depending on security's decision, see open question 1

**Unlinked clicker:** a modal asks for their GitHub login. On submit, JotFlow checks:
1. **The shape,** with the existing `check_github_login`.
2. **Not linked to someone else.** Then the modal shows "That login is linked to someone else in Slack. If it's yours, use `/jotflow verify`", the existing #195 path.
3. **The account,** through the read-only runner, with one new fixed call: `GET /repos/{owner}/{name}/assignable/{login}`. The runner asks GitHub `GET /users/{login}` and `GET /repos/{owner}/{name}/assignees/{login}`, and answers `{"account": "user" | "organization" | "missing", "assignable": bool}`, in a strict model. The repo is the proposal's own, resolved through the channel's catalog as #273 does.

Each answer has a named error in the modal:

| Check | Error |
|---|---|
| No such account | "There's no GitHub account named octo-example." |
| An organization | "octo-example is a GitHub organization, not a person." |
| Not assignable | "octo-example can't be assigned issues in acme-co/web. Ask a maintainer to add them to the organization or the repo." |
| A rate limit or GitHub failure | "GitHub couldn't be checked right now. Try again in a minute." It never reads as "no such account". |

On success the link is stored self-declared and unverified, exactly as `/jotflow link` stores it, and the add goes ahead as for a linked clicker. The modal says the link is unverified, and that `/jotflow verify` removes "(unverified)".

**Why "assignable" rather than "a member of the org":**
- The issue asks for org membership. But what GitHub actually accepts is an assignee who can be assigned in that repo: an org member with access, or an outside collaborator. So `GET /repos/{owner}/{repo}/assignees/{login}` answers the question that matters, and works for repos owned by a person too.
- It should need no new permission: the read token already has **Issues: Read-only**, which GitHub documents as enough for this call. The build confirms it against a real token before relying on it.
- A true membership check, `GET /orgs/{org}/members/{login}`, needs **Members: Read-only** on the token, and gives false answers for private memberships without it.
- See open question 2.

### 4. `jotflow identity link`

```
docker compose exec jotflow jotflow identity link --file /data/links.csv
docker compose exec jotflow jotflow identity link U0123ABCD octo-example
```

The file is CSV with a `slack_member_id,github_login` header. Every line is checked first, and nothing is written if any line fails:
- the Slack ID shape and the login shape
- no Slack ID or login twice in the file
- no login already linked to a different person, unless `--replace` is passed. Then that person is told privately, as `identity unlink` does, and the removal is recorded as by `operator`.

**Stored with `linked_by = "admin"`**, in migration 17, which adds `linked_by` to `identity_links`. Existing rows read as `"self"`. `/jotflow verify` still sets `verified` and `verified_at`.

**On the card,** an admin link shows "(linked by an admin)" rather than "(unverified)". It counts like a verified link for open question 1.

The command prints counts only, never logins: linked, unchanged, replaced, refused. `--check` also asks the drafting runner whether each account exists. It never checks whether each is assignable, since no repo is in play.

### 5. Runner and client

- **Runner:** `GET /repos/{owner}/{name}/assignable/{login}`, drafting-only, with path parameters only. It validates all three shapes, or answers 400.
  - It answers 200 with the strict model.
  - A rate limit is 503, and anything else from GitHub is a fixed 502.
  - It never follows redirects, as #273's call doesn't.
- **Client:** `RunnerClient.assignable(owner, name, login)` reads it strictly. A 503 or 502 becomes "couldn't check", as `GithubBusy` does for #273.

## Decisions

The lead and security decided on 2026-10-29:

1. **Both checks.** "Can be assigned in this repo", through `GET /repos/{o}/{r}/assignees/{login}`, is always required. When `[identity] github_org` is set, org membership through `GET /orgs/{org}/members/{login}` is required too. If the read token can't see private memberships, the check fails closed, with a reason telling the admin to grant **Members: Read-only**.
2. **Migration 17.** #304 has 16. The lead swapped #309 to 18 on 2026-10-29, so no number is left empty.
3. **A self-add on a filed issue files at once only when all of these hold:**
   - a workspace member, not a guest or external, in an unpaused channel
   - adding exactly their own login and nothing else
   - in open mode, or the clicker is an approver
   - their link is verified (#195) or admin-linked, and its stored GitHub account id equals the id the runner's assignable check returns just now
   - within a per-person cap of 5 self-adds an hour
   - through the normal execution path, as exactly one planned write adding that login. It's hash-locked, `approved_by` is the clicker, and paused is re-checked when the run starts
4. **Otherwise the self-add goes on a card,** as a suggested assignee for an approver or the next revision to confirm. That includes every unverified, modal-entered link, a mismatched account id, and approvers mode with a non-approver. The card names whose claim it is: "Alex says this is their GitHub account (unverified)". The modal offers `/jotflow verify` as the way to make future adds instant.
5. **Admin links** store the GitHub account id at link time, looked up through the runner, and are matched the same way. They're shown as "(linked by an admin)".
6. **The assignable check is rate-limited** per Slack user, 10 in 10 minutes, and the modal takes one login per submit, since "exists but can't be assigned" tells a member whether a login can reach the repo.
7. **Pinned by a test:** Alex opens the modal with Chris's login and clicks Add me on a filed issue. A card is posted, and no execution run starts.

## Delivery

**Done so far:** the runner's person checks, in #316: `/accounts/{login}`, `/repos/{o}/{n}/assignable/{login}` and `/orgs/{org}/members/{login}`, with their `RunnerClient` methods.

**Notes from #316's review, for the Add me PR:**
- **Rate limits** go in front of all three checks, per Slack user, not only the assignable one. Account existence and org membership reveal things too (security).
- **The org check's permission:** with a fine-grained read token, `/orgs/{org}/members/{login}` needs the org's **Members: Read-only** permission. Without it every check comes back `hidden`, and every unlinked person is refused. The modal's error and the setup docs name that permission (reviewer).
- **What that permission widens:** with **Members: Read-only**, the read token can see every private membership in the org. install.md and the threat model say so where the permission is documented (security).

Three PRs, each closing part of #305, with the last saying Fixes #305:

1. **Links:** migration 17 (`linked_by`), `jotflow identity link` with the account id stored, and "(linked by an admin)" on cards.
2. **Reasons:** `AssigneeReason`, `owner_hints`, the write tools' checks, drafting.md, and the card's reasons, with the "add me" replay fixture.
3. **Add me:** the buttons, the modal, the runner's assignable and org calls and the client, self-add per decisions 3 and 4, and the caps.

## Risks and open questions

1. **Decided above (3, 4).** The original proposal was: My proposal: yes, only when all of these hold:
   - the clicker is a workspace member, not a guest or external, in an unpaused channel
   - the change adds exactly the clicker's own login and nothing else
   - their link is verified (#195) or admin-linked
   - the runner confirms just now that the login is assignable in that repo

   Then JotFlow files the update at once, recorded as approved by the clicker. Otherwise, including every unverified, self-declared link, it posts a normal approval card. The thread gets a short note either way: "Alex added themselves as an assignee" or "Alex asked to be added. An approver can approve it." Assigning yourself gives you no new access on GitHub, since an assignee must already have access, and the verified-link condition stops anyone adding someone else's account. The alternative, every self-add carded, is safer but slower.
2. **Decided above (1): both.** The original question was Recommended: assignable, for the reasons in section 3. A membership check could be added later behind an `[identity] github_org` setting that needs **Members: Read-only**.
3. **Owner hints assign someone who isn't in the thread.** They're still only ever a proposal, they need a linked login, and a person approves. The card names the reason.
4. **Admin links** are trusted like verified ones. The bulk file is run by whoever runs JotFlow, who already holds every secret. But a wrong line assigns the wrong person, so `--check` and the private note on `--replace` matter.
5. **The thread reply gains a button.** Its text is unchanged for `jotflow rebuild`, and a test pins that.

## Progress checklist

- [x] Security and the lead decide open questions 1 and 2
- [x] Commit this plan and open the draft PR
- [x] Models: `AssigneeReason`, reasons on both proposal kinds, and `owner_hints` (#325)
- [x] The write tools' reason checks, and drafting.md (#325)
- [x] The card's reasons (#325), and Add me, plus the filed-reply blocks and their button
- [x] The modal, the runner's assignable and org calls (#316), and the client
- [x] Migration 17 and `jotflow identity link` (#321, #328)
- [x] Self-add on a filed issue, per decisions 3 and 4
- [x] Tests:
  - replay fixtures for a volunteer on a draft and on a filed issue, with an injected line that must assign no one (#325)
  - card rendering, the click rules, the modal's errors against a fake GitHub, the caps, the rebuild text pin, and decision 7's test, in `tests/test_add_me.py`
- [x] architecture.md, usage.md, install.md's Members permission, the threat-model rows for self-add and the org permission, the example config, and the CHANGELOG
- [x] CodeRabbit once, `make check`, mark ready

## Validation

- `tests/test_add_me.py` runs every rule above against a fake GitHub, on both stores where the store matters: the card add, the dialog's named errors and fail-closed org check, both rate limits, instant and carded filed adds, decision 7, auto mode, and supersede.
- The filed reply's `text` is pinned to what `jotflow rebuild` reads.
- `make check` passes.

**How Add me is built:**
- A filed issue's self-add is an update proposal with `run_id = "add-me"` and the clicker as its only participant. The store counts these for the hourly cap, a new draft never supersedes one, and auto mode never approves one.
- The dialog acks within Slack's three seconds with "Checking GitHub…", then updates itself with the outcome once the runner answers.
- The runner-call limit, 10 in 10 minutes per person, is kept in memory, so a restart resets it. The hourly self-add cap is in the store.
- A linked person's add on a card asks GitHub nothing, since an approver still approves the card.

## Handoff notes

All examples are fictional: `acme-co/web`, `octo-example`, `U0123ABCD`.
