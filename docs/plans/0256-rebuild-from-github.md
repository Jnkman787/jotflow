# `jotflow rebuild --from-github`: titles and lost threads from issue markers

## Links and status

- Issue: #256 (M6), a follow-up to #38
- Draft PR: to be opened
- Status: built, ready for review
- Last verified: 2026-10-20, against main
- Next safe action: the lead's review, then security's on the new runner endpoint

## Objective and done-when

`jotflow rebuild` (#38) restores the index from JotFlow's own Slack replies, but two things are lost:
- **Real topic titles.** Rebuilt topics are titled `web#42`, so the drafting agent's topic matching works from a number, not words.
- **Threads whose "Created" reply is gone,** because it was deleted, or older than Slack's retention. Their messages come back unlinked, and a follow-up starts a new issue.

GitHub still has both. Every issue JotFlow wrote carries `<!-- jotflow:proposal:ID -->`, its title, and, with `issue_links = "permalinks"`, the Slack permalinks of its source messages.

Done when, from #256's boxes:

- [ ] **The command.** `jotflow rebuild --from-github` asks the drafting runner for JotFlow's own issues in each channel's repos. It sets real titles on the topics rebuild restored, and restores topics and message links for issues whose Slack reply was lost
- [ ] **No token in JotFlow.** JotFlow still holds no GitHub token. The runner makes one fixed, read-only GitHub call per page, with the read token, and returns pointers, the author's login and a cleaned title only. It never returns the body or comment text. The threat model has a row for it
- [ ] **Optional.** It's off by default, documented as optional, and plain `jotflow rebuild` is unchanged
- [ ] **Tests:** a fake runner and a fake GitHub, a round trip, every refusal, and no issue body or comment text in the store, the logs or the HTTP reply. `make check` passes

## Design

### No agent run: a fixed runner call

#256 suggested a new read-only run kind. A fixed runner endpoint is the better fit, and is how #236 and #195 already reach GitHub from the drafting runner (`GET /repos/{owner}`, `POST /verify`):
- **No model and no prompt.** There's nothing to inject, and nothing an issue's text can steer.
- **The read token** stays in the runner, as for every other GitHub read.
- **The result is deterministic,** and testable with a fake GitHub.

### The runner endpoint

**`POST /repos/jotflow-issues`, with a strict body `{owner, name, since}`** and no query string, since the runner gate refuses one. It's on the drafting runner only: the execution runner returns 404, as `/repos/{owner}` does. It sits behind the same secret gate. This replaces the GET with `?since=` first drafted here, per security's blocker 1. A second fixed call, **`GET /identity`** on the execution runner, returns only its write token's login, from GitHub's `GET /user`.

1. **It checks the name** against `is_owner_name` and `is_bare_repo_name`, refusing with 400 otherwise.
2. **It pages GitHub's `GET /repos/{owner}/{name}/issues?state=all&per_page=100`,** pull requests excluded, newest first. It's bounded: at most 50 pages (5,000 issues), plus GitHub's `since` when given, so a giant repo answers with `"truncated": true` rather than running forever.
3. **For each issue whose body holds a JotFlow marker line,** matched with the exact `_MARKER_LINE` pattern execution uses, it returns only:
   - `number`, and `state`: open or closed
   - `author`: the issue author's login, so JotFlow can keep only issues its write token wrote
   - `title`, cleaned with `clean_title` and cut to 80 characters, the same rule as every topic title
   - `markers`: the proposal ids, `[A-Za-z0-9_-]{1,64}`
   - `sources`: each Slack permalink in the footer, matched with the exact `_PERMALINK` pattern and parsed to `{channel_id, ts, thread_ts}`. The workspace host is dropped. At most 50 per issue.
4. **It never returns the body,** comments, labels, assignees or anything else. The parsing happens in the runner, so issue text never crosses to JotFlow.
5. **Errors:**
   - GitHub refusing or rate limiting is a 502 with a fixed message.
   - The runner logs counts and the repo, never text.
   - A rate limit names the reset time only.

### What rebuild does with it

`--from-github` runs after the Slack walk, for each configured channel:

1. **The repos** are the channel's listed repos, plus the repos its #236 scopes find through the same runner's `GET /repos/{owner}`.
2. **Each repo** is asked once, so a repo that feeds two channels is read once and applied to each.
3. **Each marked issue** is applied, but only if its `author` is the write token's login from `/identity`. Anyone can write a marker line, so any other issue is skipped and counted.
   - **A real title.** If a topic in this channel is already bound to `repo#number`, its `nick#N` title is replaced. A title a person or the agent already gave, anything not of the form `nick#N`, is never overwritten.
   - **A lost thread.** If no topic is bound to the issue, and some of its `sources` are in this channel, rebuild creates one, titled with the issue's title and bound to it. It links each source message as the agent's decision, via `link_message` with `by="agent"`, so a person's later decision still wins, skipping any message that's already linked. It links only messages the Slack walk saw in this channel, using the thread Slack reported. Plain rebuild's own Slack-reply links keep their `by="person"`.
   - **Another channel's sources** are never used for this channel.
4. **Its own summary line** gives titles set, topics restored, messages linked, issues skipped because someone other than JotFlow wrote them, and issues skipped because they named no source the walk saw in this channel.

**Limits it documents:**
- **With `issue_links = "none",`** bodies carry the marker but no permalinks, so `--from-github` can set titles but can't restore a lost thread.
- **Updates write their marker in a comment.** Comments aren't read, so an update's thread comes back only through its Slack reply. A follow-up could add a bounded comment read.
- **Issues moved to another repo, or deleted,** are gone.

### Running it

```
docker compose stop jotflow
docker compose run --rm --no-deps jotflow jotflow rebuild --from-github
docker compose start jotflow
```

`--no-deps` keeps the run from starting other services, so the drafting runner (`agent`) must already be running. Rebuild checks `GET /info` first. If the runner isn't reachable, or doesn't serve drafting, it refuses by name before reading Slack. Without `--from-github`, it never contacts a runner.

## Security's review, 2026-10-20

These change the design above where they differ.

- **Blocker 1: no query string.** The runner gate refuses any query string on runner endpoints (#236 condition 1). The endpoint is therefore `POST /repos/jotflow-issues`, with a strict body `{owner, name, since}`, extra fields forbidden. GitHub's own `?state=all&page=N&since=` is built outbound by the runner, which is fine.
- **Blocker 2: forged markers.** Anyone who can open an issue, which on a public repo is anyone, can write a body with a marker line and permalinks to this channel's messages. Two fixes:
  - **Only JotFlow's own issues count.** The runner returns each issue's `user.login` and `user.type`. JotFlow accepts only issues authored by the write token's account, which it learns from a new fixed call on the execution runner, `GET /identity`. That returns only the login, from GitHub's `GET /user`, and the write token never leaves the runner. Any other issue is skipped and counted.
  - **Restored links are the agent's,** with `by="agent"`, not a person's, so a real person's later decision still wins.
- **Condition 1.** Link only messages the Slack walk saw in that channel's history and threads. A permalink naming any other ts is skipped, so a footer can't bind a message id that doesn't exist.
- **Condition 2: #236's GitHub client.** That means no redirects, `trust_env=False`, a timeout, a byte cap while each page streams, `page=N` built locally rather than read from the Link header, and pull requests dropped by their `pull_request` key.
- **Condition 3: strict validation on JotFlow's side.**
  - an `extra="forbid"` wire model
  - caps on issues, on sources and on bytes
  - numbers greater than 0, and state open or closed
  - marker ids, channel ids and ts checked by their existing patterns
  - the title cleaned again
- **Condition 4: a threat-model row.** Forged markers are refused by the author check. A collaborator who edits a JotFlow-authored body can still change its permalinks, but that's within the repo's write trust, and condition 1 bounds it to messages that exist in the channel.
- **The stored title is acceptable,** given the author check. A JotFlow-authored issue's title was approved by a person, and only triage-or-higher users can edit it. It's cleaned, capped, marked untrusted in the packet, and only ever replaces a `nick#N`.

## Risks and open questions

1. **For security: the new endpoint.** It's the read token reaching GitHub on JotFlow's request, for any configured or scoped repo. The safeguards:
   - It returns parsed pointers and a cleaned title only.
   - Repo names are validated.
   - The drafting runner only.
   - Pages are bounded.

   Is a GitHub-written title acceptable in the store, given that topic titles are already agent-written untrusted text, cleaned the same way?
2. **Decided by the lead on 2026-10-20: a fixed endpoint, not a run kind.** There's no model and no prompt to inject. #256's boxes are reworded, with a dated note.
3. **Cost.** A repo with 5,000 issues is 50 calls, well inside the read token's 5,000 an hour. `--since` passes through to cut it.
4. **A token that reaches more repos than one channel feeds.** Rebuild asks only for the channel's own repos, and applies sources only to the channel they name. The runner doesn't know channels, so the scoping is JotFlow's, as it is for discovery.

## Progress checklist

- [x] The lead approves the plan and answers question 2
- [x] Security answers question 1, with two blockers and four conditions, all taken
- [x] The runner endpoint, with `github_repos`-style client code and the marker and permalink parsing reused from execution
- [x] Rebuild's `--from-github`: the info check, the repo set with scopes, titles and lost threads
- [x] Tests: the endpoint, with a fake GitHub (paging, truncation, PRs excluded, no text returned, a 502); rebuild with a fake runner (titles, lost threads, other channels' sources ignored, issue_links none)
- [x] Docs: deployment.md's rebuild section, the threat model row, architecture.md's runner endpoints, the CHANGELOG
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- `make check` passes.
- **A round trip:**
  - delete a thread's "Created" reply from the fake Slack
  - rebuild without and with `--from-github`
  - assert the second restores the topic, its title and its messages
  - assert no issue body text reaches the store, the logs or the runner's reply

## Handoff notes

- **The modules:**
  - `adapters/github_issues.py`, on the runner side: `marked_issues`, `parse_issue` and `token_login`, reusing `github_repos`' client and `_page`
  - two runner routes: `POST /repos/jotflow-issues`, routed before `/repos/{owner}` so it isn't taken for an owner, and `GET /identity`
  - `RunnerClient.marked_issues` and `write_login`, with strict wire models in models.py and the title cleaned again
  - `rebuild.apply_github`, with `ChannelPlan.seen`, every person's message the walk saw, mapped to its thread
- **Login checks** use Python's `re` in validators, `is_github_login`, because pydantic's pattern engine has no lookahead.
- **Restored links take their thread from the Slack walk,** not from the issue's permalink.
- **For security's follow-up question.** Plain `rebuild` links its Slack-reply threads with `by="person"`, from #255. `--from-github` uses `by="agent"`, as security asked. Whether plain rebuild should also use the agent's level is a separate change, if wanted.
