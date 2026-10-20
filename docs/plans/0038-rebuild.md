# `jotflow rebuild`: rebuild the store from Slack

## Links and status

- Issue: #38 (M6: After v0.1.0)
- Draft PR: to be opened
- Status: plan only, for the lead before any code
- Last verified: 2026-10-20, against main
- Next safe action: the lead's review, then the open questions

## Objective and done-when

The README calls the store a rebuildable cache. Today that's only a design property: losing `jotflow.db` without a backup loses every topic, so follow-ups in an old thread start new issues, and `re:` keeps working only by issue number. `jotflow rebuild` makes the claim true for what matters most: which threads and messages belong to which issue.

Done when:

- [ ] On a stopped JotFlow with an empty or missing database, `jotflow rebuild` walks each configured channel and restores:
  - **topics:** one per thread that JotFlow answered with an issue link, with the repo and issue number
  - **message links:** the human messages in those threads, linked to their topic
  - **channel modes:** the latest mode JotFlow announced in each channel
  - **the help guide:** whether it was already posted, so it isn't posted and pinned twice
- [ ] It stores pointers only. Message text is read in memory to recognise JotFlow's own replies, and never stored or logged
- [ ] It refuses a database that already holds topics, unless given `--merge`, which adds only what's missing and never overwrites
- [ ] It prints what it restored per channel, with counts only, and what it couldn't
- [ ] docs/deployment.md's restore section and docs/install.md document it, with its limits
- [ ] Tests run it against a fake Slack and compare the restored store with the original. `make check` passes

## What JotFlow leaves in Slack, to rebuild from

All of these are JotFlow's own bot messages, identified by the bot's user id from `auth.test`, never by text alone:

| Store data | Evidence in Slack |
|---|---|
| A topic linked to an issue | The thread reply `Created <https://github.com/o/r/issues/N\|nick#N>` or `Updated …` (thread_notice.reply_text). The URL gives the repo and number |
| The topic's title | Nothing reliable. Once the draft is cleared after the write, the card's header is "Proposed issue" and the title section is gone (cards.py). A rebuilt topic is titled by its issue, such as `web#42`, using the repo's nickname from jotflow.toml |
| Which messages belong to it | The thread's human messages, other than chatter (see below) |
| A channel's mode | JotFlow's latest mode announcement in the channel (modes.py MEANING) |
| The help guide | JotFlow's own guide message in the channel (help.py guide_text) |

## What it can't rebuild, by design

These are documented in deployment.md:

- **Pending, approved or failed proposals.** Their drafts are gone, and a card can't be trusted as a draft, because it's text JotFlow would have to re-read. Their threads are re-drafted the next time someone posts there. The cards stay in Slack, but their buttons refuse as unknown.
- **Topic titles,** as question 2 explains.
- **Proposal history for applied ones:** the approver, the content hash and the run records. The topic-to-issue link, which is what routing needs, is restored.
- **Identity links, operator tokens, and identity unlinks.** Slack only ever showed these privately. People run `/jotflow link` again, and admins reissue tokens.
- **Chatter marks and reaction marks.** A rebuilt thread's messages are all linked as ideas, except JotFlow's own and messages starting `skip:`. Old reactions aren't replayed.
- **Messages deleted in Slack,** and threads older than the workspace's history retention, or older than `--since`.
- **Threads JotFlow drafted but never wrote an issue for.** There's nothing to point at.

## Design

- **The command.** `jotflow rebuild [--since 90d|all] [--channel C…] [--merge] [--dry-run]`, run on the host with JotFlow stopped:

  ```
  docker compose stop jotflow
  docker compose run --rm jotflow jotflow rebuild
  docker compose start jotflow
  ```

  It refuses while the heartbeat file is fresh, because a running JotFlow would race it. `--dry-run` reports counts and writes nothing.
- **Slack calls.** Each one uses scopes the app already has:
  - `conversations.history` for the channel, paged, newest first, back to `--since`. The default is `all`.
  - `conversations.replies` for each thread with replies.
  - `auth.test` once, for the bot's id.

  It follows Slack's tier-3 limits and `Retry-After`, with progress lines per channel. A large channel takes minutes, not seconds, and the docs say so.
- **The store** is written through the normal store API: `create_topic`, `set_topic_issue`, `link_message` with `linked_by='person'`, plus the mode and the help notice. Each thread goes in one transaction, so a crash leaves whole threads. Running again with `--merge` continues.
- **Permalinks** are built the way ingest builds them today, so issue-body links and stored pointers agree.
- **Logging.** It logs channel ids, thread ts and counts, never text. It uses the same redaction as `jotflow run`.

## Risks and open questions

1. **GitHub markers, the other half of #38's idea.** Every issue body carries `<!-- jotflow:proposal:ID -->`, and with `issue_links = "permalinks"`, its source permalinks. They could restore a topic whose thread reply was deleted. But JotFlow holds no GitHub token, by D6 and the threat model, so reading GitHub would need a new read-only run kind on the drafting runner. I'd leave GitHub out of v1 of rebuild, and say so. A follow-up could add `--from-github` through the runner.
2. **Topic titles are lost.** Rebuilt topics are titled `nick#N`. The agent's `list_topics` then sees no descriptive titles for old topics, so its topic matching leans on `get_topic` and on duplicate search through GitHub. A `--from-github` follow-up, as in question 1, could restore real titles through the read-only runner. Rebuild reads no card or message text to make a title, so no draft text is re-read.
3. **Several issues in one thread** happen when a thread got a create, then an update, or two topics. Each reply's issue becomes its own topic only if no topic already links that issue. Messages are linked to the topic of the nearest preceding reply.
4. **`--since all`** on a big, old channel is slow. Should the default be `topic_window_days`, with `all` opt-in? I'd default to all, since a restore is rare and should be complete.

## Progress checklist

- [ ] The lead reviews this plan, and the architect answers questions 1 and 2
- [ ] The command, the Slack walk, and the store writes
- [ ] Refusals: running JotFlow, non-empty store without `--merge`
- [ ] Tests with a fake Slack: round trip, deleted reply, two issues in one thread, rate limits, `--dry-run`
- [ ] Docs: deployment.md restore, install.md, threat model data table if anything changes
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

- `make check` passes.
- **A round-trip test.** Replay a fixture into a store, record its topics, links and modes, delete the store, and rebuild from the fake Slack's record of what JotFlow posted. The restored rows match, apart from the documented limits.

## Handoff notes

None yet.
