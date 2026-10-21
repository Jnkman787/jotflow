# A channel-scoped issue lookup for question runs

## Links and status

- Issue: #273 (M6), a follow-up to #57 and #269
- Draft PR: to be opened
- Status: plan only, for security and the lead before any code
- Last verified: 2026-10-21, against main
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
   - `number`, plus `state` (open or closed) and `state_reason` (completed, not_planned, reopened or null)
   - `title`, with control characters removed, capped at 256
   - `labels`: their names only, at most 20, each checked by the existing `label_problem` rule. Anything else is dropped
   - `comments`, the count only
   - `updated_at`, a timestamp
   - **Never** the body, comments' text, assignees, author or URLs
5. **Errors.** A 404 or a 410 from GitHub is answered 404. Anything else is a fixed 502. Logs give the repo and number, never text.

### JotFlow: a question-only MCP tool

**`get_issue(repo, number)`** is a read tool for `RunKind.QUESTION` only. Drafting keeps its own GitHub server, and operators don't get it.

1. **The channel comes from the run's token.** The grant names its channel, and the agent never chooses it.
2. **The repo must be in that channel's catalog.** `repo` resolves through `RepoCatalog.repo(grant.channel_id, repo)`, which is what `propose_issue` and `re:` already use. That accepts `owner/name` or a nickname, in any case, and covers listed and scope-found repos. Anything else is refused before any runner call, with a fixed reason: "not one of this channel's repos". The call then goes out with the catalog's own spelling.
3. **One runner call.** `RunnerClient.issue(owner, name, number)` reads a strict reply model with a size cap, and cleans the title again.
4. **The result,** to the agent: `repo`, `number`, `state`, `state_reason`, `comments`, `updated_at`, plus `title` and `labels` as `UntrustedText`. A 404 becomes "no such issue in this repo", so the agent can say so.
5. **Capped at 5 calls per run,** through the existing tool-call ledger, so a looping agent can't spend the read token's hourly limit.

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
3. **For the lead: no caching.** Each call reads GitHub live, so answers say "just now". A short cache, around 60 seconds, could come later if the cost shows.
4. **Rate.** At most 5 calls per question run, and question runs are already capped at 10 per channel per hour, so at most 50 reads an hour per channel.

## Progress checklist

- [ ] Plan reviewed by security and the lead
- [ ] Runner endpoint and `github_issues`-style call
- [ ] `RunnerClient.issue`, the strict model, and the title re-cleaned
- [ ] The `get_issue` read tool for question runs, with the catalog check and the per-run cap; presets
- [ ] question.md, usage.md (the #273 line becomes real), architecture.md, threat model, CHANGELOG
- [ ] Tests: the endpoint against a fake GitHub (PR 404, no body in the reply, a 502), the client's strict model, two channels refusing each other's repos, the cap, and a hostile title and label through safe_text
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- `make check` passes.
- The two-channel test, and the hostile-text tests above.

## Handoff notes

None yet.
