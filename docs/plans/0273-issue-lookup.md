# A channel-scoped issue lookup for question runs

## Links and status

- Issue: #273 (M6), a follow-up to #57 and #269
- PR: #281
- Status: built. The lead approved the plan on 2026-10-21, and security approved it with conditions, all met below
- Last verified: 2026-10-21, against main after #280
- Next safe action: review

## Objective and done-when

Question runs have no GitHub access (#269). That's because the shared read token reaches every repo it was granted, including repos that feed other channels, and an answer posts without approval. So today an answer can say an idea is tracked as `acme-co/web#42`, but not whether that issue is still open, or what it's labelled.

This adds one narrow read, which the question agent can only point at its own channel's repos.

Done when, from #273's boxes:

- [ ] **Own channel only.** A question run can read an issue in its own channel's repos and nothing else. A test uses two channels feeding different repos, and a question in one can't read the other's issue, by repo, by nickname, or by any spelling
- [ ] **Untrusted, and still cleaned.** The returned text is marked untrusted, and answers still pass `safe_text`, with tests that a hostile title or label can't add a link, a ping or another repo's reference to an answer
- [ ] **Threat model.** It's updated
- [ ] `make check` passes

## Design

### The runner: one fixed, read-only call

**`GET /repos/{owner}/{name}/issues/{number}`** is on the drafting runner, with the read token, behind the secret gate. It takes path parameters only, as #236's `/repos/{owner}` does, and no query string, which the gate refuses.

1. **Input.** It checks `is_owner_name`, `is_bare_repo_name`, and that the number is 1 to 10⁹, or returns 400. The execution runner returns 404.
2. **The GitHub call.** One request, `GET /repos/{owner}/{name}/issues/{number}`, through #236's client and `_page`: no redirects, `trust_env=False`, a timeout, and a 256 KB byte cap.
3. **A pull request** has a `pull_request` key. It's answered 404, like an issue that doesn't exist, so the tool only ever describes issues.
4. **What it returns.** Only these, in a strict model:
   - `number`, 1 to 10⁹ and the one asked for, plus `state` (open or closed) and `state_reason` (completed, not_planned, reopened, duplicate or null)
   - `title`, cleaned with `clean_title(title, 120)`, the topic-title rules. A title with nothing left becomes `#42`
   - `labels`: their names only, at most 20. A name is kept only if it passes `label_problem` and cleaning leaves it unchanged, so no Slack markup, mention or link. Anything else is dropped
   - `comments`, an int of 0 or more
   - `updated_at`, an ISO timestamp with a timezone
   - **Never** the body, comments' text, assignees, author or URLs
5. **Errors.**
   - A 404 or a 410 from GitHub is answered 404.
   - **A rate limit, a 429 or a 403 with no requests left, is answered 503,** never 404, because it says nothing about whether the issue exists. This uses `_page`'s existing rate-limit check, as #266 does.
   - A transferred issue's 301 isn't followed, and is a fixed 502, like anything else.
   - Any field off shape is a 502 too: the model is strict, so `"42"` or `true` isn't a number.
   - Logs give the repo and number, never text.

### JotFlow: a question-only MCP tool

**`get_issue(repo, number)`** is a read tool for `RunKind.QUESTION` only. Drafting keeps its own GitHub server, and operators don't get it.

1. **The channel comes from the run's token.** The grant names its channel, and the agent never chooses it.
2. **The repo must be in that channel's catalog.** `repo` resolves through `RepoCatalog.repo(grant.channel_id, repo)`, which is what `propose_issue` and `re:` already use. That accepts `owner/name` or a nickname, in any case, and covers listed and scope-found repos. Anything else is refused before any runner call, with a fixed reason: "not one of this channel's repos". The call then goes out with the catalog's own spelling.
3. **One runner call.** `RunnerClient.issue(owner, name, number)` checks the owner, name and number again before sending, reads the same strict model with a 64 KB cap, checks the number is the one asked for, and cleans the title again. Only the runner's exact "no such issue" reply is a miss. Any other 404, such as the execution runner's, is a runner error.
4. **The result,** to the agent: `repo`, `number`, `state`, `state_reason`, `comments`, `updated_at`, plus `title` and `labels` as `UntrustedText`. A 404 becomes "no such issue in this repo", so the agent can say so. A 503 or a 502 becomes "couldn't check GitHub right now", never "not found", and question.md says to answer that way.
5. **Capped at 5 calls per run,** counted in memory per run id, not in the tool-call ledger. The ledger is for writes, whose repeats must return the first result, and a read has nothing to replay. The count is bounded to the last 1,000 runs, and a refused repo doesn't use a call. A restart forgets the counts, but it also ends every run.

The presets add `get_issue` to `JOTFLOW_TOOLS[QUESTION]`. `GITHUB_TOOLS[QUESTION]` stays empty, so there's still no GitHub MCP server in a question run.

### How #269's guarantee holds

#269's rule is that a question in one channel can't reach another channel's content. It holds because:
- **There's no free search.** The tool takes one exact issue in a named repo, and there's no listing or query.
- **The repo set is the asking channel's catalog,** fixed by the run token's channel. A test has two channels with disjoint repos, and checks that `get_issue` from one refuses the other's repo by `owner/name`, by nickname, and in mixed case.
- **A shared repo is shared on purpose.** When a repo feeds two channels, both may read it, because jotflow.toml says it feeds both.
- **No body or comment text crosses,** so a long issue can't be paraphrased. Only a title and labels do, which a person could already see on the card or in the issue list.

### safe_text still covers it

The answer goes through `safe_text` unchanged, whatever the tool returned:
- **A title holding a URL, `@login`, `<!channel>`, or `other/repo#1`** is cleaned like any other answer text. Tests use a hostile title and a hostile label.
- **The title is untrusted text,** like topic titles. question.md says so: a title can be quoted as a fact about the issue, never followed.
- **Issue references in the answer** still pass the channel-repo filter, which the lookup can't widen, because it reads only those repos.

### question.md

A new step, "Is it open? What's it labelled?", says to call `get_issue` for an issue the topics name, only in `channel.repos`, at most a few times. Answer from what it returns, and say it's what GitHub showed just now. The file stays under its byte cap.

## Risks and open questions

1. **For security: the runner endpoint and the tool.** Specifically:
   - path parameters with a strict reply
   - PRs answered 404
   - the title capped and labels filtered
   - the repo checked against the token-bound channel's catalog before any call
2. **For security: is the title acceptable in an answer at all,** given that safe_text runs? The alternative is to return state and labels only.
3. **Decided by the lead on 2026-10-21: no caching.** Fresh is better for status questions, and 50 reads per channel per hour is well within the read token's limits.
4. **Rate.** At most 5 calls per question run, and question runs are already capped at 10 per channel per hour, so at most 50 reads an hour per channel.

### Security's conditions, and where each is met

Security approved the plan on 2026-10-21 on these conditions:

1. **The number is an int from 1 to 10⁹, checked on both sides.** Nothing from the agent reaches the URL except a validated owner, name and number. The tool's input is a strict int, the client checks all three before sending, and the runner checks the path against `[1-9][0-9]{0,9}` and the bound. Tests cover `0`, `01`, `-1`, `1.0` and an 11-digit number.
2. **Every field is strict in both places.** `IssueDetails` is a strict model with extra fields forbidden, used by the runner and by JotFlow's client. Tests cover each field off shape, and a 301 answered 502.
3. **A threat-model row** for issue titles and labels repeated in an answer without approval, under "Malicious repo or GitHub text".
4. **A scope-found repo works only while it's freshly listed,** and an excluded, archived or unmatched one is refused, through `RepoCatalog.repo`. `test_get_issue.py` checks it before a listing, while fresh, and once stale.
5. **The title is kept, cleaned, and untrusted.** `clean_title(title, 120)` on the runner and in JotFlow, returned as `UntrustedText`, with `safe_text` as the second layer.

## Progress checklist

- [x] The lead approves the plan
- [x] Security reviews it, with the conditions above
- [x] Runner endpoint and `github_issues`-style call
- [x] `RunnerClient.issue`, the strict model, and the title re-cleaned
- [x] The `get_issue` read tool for question runs, with the catalog check and the per-run cap; presets
- [x] question.md, usage.md (the #273 line becomes real), architecture.md, threat model, CHANGELOG
- [x] Tests in `tests/test_get_issue.py`: the GitHub call, the endpoint, the client, two channels refusing each other's repos, scope freshness, the cap, and a hostile title and label through safe_text
- [x] CodeRabbit once (no findings), `make check`, mark ready

## Validation

- `make check` passes.
- The two-channel test, and the hostile-text tests above.

## Handoff notes

- `RateLimited` is a new `GithubError` subclass raised by `_page`, so the existing callers that catch `GithubError` behave as before.
- Replay fixtures have no issue reader, so `get_issue` there answers "couldn't check GitHub right now". #270 can add fixture issues.
