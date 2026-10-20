# `jotflow rebuild --from-github`: titles and lost threads from issue markers

## Links and status

- Issue: #256 (M6), a follow-up to #38
- Draft PR: to be opened
- Status: plan only, for the lead before any code
- Last verified: 2026-10-20, against main
- Next safe action: the lead's review, then security's on the new runner endpoint

## Objective and done-when

`jotflow rebuild` (#38) restores the index from JotFlow's own Slack replies, but two things are lost:
- **Real topic titles.** Rebuilt topics are titled `web#42`, so the drafting agent's topic matching works from a number, not words.
- **Threads whose "Created" reply is gone,** because it was deleted, or older than Slack's retention. Their messages come back unlinked, and a follow-up starts a new issue.

GitHub still has both. Every issue JotFlow wrote carries `<!-- jotflow:proposal:ID -->`, its title, and, with `issue_links = "permalinks"`, the Slack permalinks of its source messages.

Done when, from #256's boxes:

- [ ] **The command.** `jotflow rebuild --from-github` asks the drafting runner for JotFlow's own issues in each channel's repos. It sets real titles on the topics rebuild restored, and restores topics and message links for issues whose Slack reply was lost
- [ ] **No token in JotFlow.** JotFlow still holds no GitHub token. The runner makes one fixed, read-only GitHub call per page, with the read token, and returns pointers and a cleaned title only, never issue text. The threat model has a row for it
- [ ] **Optional.** It's off by default, documented as optional, and plain `jotflow rebuild` is unchanged
- [ ] **Tests:** a fake runner and a fake GitHub, a round trip, every refusal, and no issue body or comment text in the store, the logs or the HTTP reply. `make check` passes

## Design

### No agent run: a fixed runner call

#256 suggested a new read-only run kind. A fixed runner endpoint is the better fit, and is how #236 and #195 already reach GitHub from the drafting runner (`GET /repos/{owner}`, `POST /verify`):
- **No model and no prompt.** There's nothing to inject, and nothing an issue's text can steer.
- **The read token** stays in the runner, as for every other GitHub read.
- **The result is deterministic,** and testable with a fake GitHub.

### The runner endpoint

**`GET /repos/{owner}/{name}/jotflow-issues?since=<ISO date>`** is on the drafting runner only. The execution runner returns 404, as `/repos/{owner}` does. It sits behind the same secret gate.

1. **It checks the name** against `is_owner_name` and `is_bare_repo_name`, refusing with 400 otherwise.
2. **It pages GitHub's `GET /repos/{owner}/{name}/issues?state=all&per_page=100`,** pull requests excluded, newest first. It's bounded: at most 50 pages (5,000 issues), plus GitHub's `since` when given, so a giant repo answers with `"truncated": true` rather than running forever.
3. **For each issue whose body holds a JotFlow marker line,** matched with the exact `_MARKER_LINE` pattern execution uses, it returns only:
   - `number`, and `state`: open or closed
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
3. **Each marked issue** is applied:
   - **A real title.** If a topic in this channel is already bound to `repo#number`, its `nick#N` title is replaced. A title a person or the agent already gave, anything not of the form `nick#N`, is never overwritten.
   - **A lost thread.** If no topic is bound to the issue, and some of its `sources` are in this channel, rebuild creates one, titled with the issue's title and bound to it. It links each source message as a person's decision, via `link_message` with `by="person"`, skipping any message that's already linked.
   - **Another channel's sources** are never used for this channel.
4. **Its own summary line** gives titles set, topics restored, messages linked, and issues skipped because they named no source in this channel.

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

## Risks and open questions

1. **For security: the new endpoint.** It's the read token reaching GitHub on JotFlow's request, for any configured or scoped repo. The safeguards:
   - It returns parsed pointers and a cleaned title only.
   - Repo names are validated.
   - The drafting runner only.
   - Pages are bounded.

   Is a GitHub-written title acceptable in the store, given that topic titles are already agent-written untrusted text, cleaned the same way?
2. **For the lead: the run kind #256 asked for.** I'd do a fixed endpoint instead, as above. I'll reword the box if you agree.
3. **Cost.** A repo with 5,000 issues is 50 calls, well inside the read token's 5,000 an hour. `--since` passes through to cut it.
4. **A token that reaches more repos than one channel feeds.** Rebuild asks only for the channel's own repos, and applies sources only to the channel they name. The runner doesn't know channels, so the scoping is JotFlow's, as it is for discovery.

## Progress checklist

- [ ] The lead reviews the plan, and questions 1 and 2 are answered
- [ ] The runner endpoint, with `github_repos`-style client code and the marker and permalink parsing reused from execution
- [ ] Rebuild's `--from-github`: the info check, the repo set with scopes, titles and lost threads
- [ ] Tests: the endpoint, with a fake GitHub (paging, truncation, PRs excluded, no text returned, a 502); rebuild with a fake runner (titles, lost threads, other channels' sources ignored, issue_links none)
- [ ] Docs: deployment.md's rebuild section, the threat model row, architecture.md's runner endpoints, the CHANGELOG
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- `make check` passes.
- **A round trip:**
  - delete a thread's "Created" reply from the fake Slack
  - rebuild without and with `--from-github`
  - assert the second restores the topic, its title and its messages
  - assert no issue body text reaches the store, the logs or the runner's reply

## Handoff notes

None yet.
