# Saved threads

Each `.json` file here is a saved Slack thread in the `jotflow-thread/1` format. Tests run every file through JotFlow's real ingest filter. `jotflow replay` (#17) will feed them through the store and the tool layer with a scripted fake agent. They ship with the package, so they also work as demos without a clone.

Everything in them is fictional: the Acme Co workspace, the `C0EXAMPLE1` channel, the `acme-co` repos, and the people. A test fails if a file contains a real-looking Slack id, a URL or email outside the fictional domains, a repo owner other than `acme-co`, or anything shaped like a token.

## The fixtures

| File | What happens |
|---|---|
| `feature-request.json` | A clean feature request with one supporting reply. One new topic and one `create_issue` proposal for `acme-co/web`, labelled `enhancement`. |
| `bug-report.json` | A bug report, a confirmation, a likely cause, a thank-you, and a `:bug:` reaction. One topic with all four messages and one `create_issue` proposal labelled `bug`. |
| `chatter.json` | Lunch plans and a greeting, all marked chatter: no topic, no proposal. Ingest drops an edit, the bot's own message, a channel join, and a redelivered event before any agent sees them. |
| `same-topic-two-threads.json` | Two threads, half an hour apart, both asking for a dark mode. The second thread joins the first thread's topic, so there is one topic and one proposal. |
| `existing-topic.json` | A new message about the CSV export, which already has a topic and issue `acme-co/web#42`. The agent links it to that topic and proposes a comment on #42 instead of a new issue. |
| `keywords.json` | `idea:` is a hint the agent follows. `re: web#42` is attached by JotFlow, and the agent proposes an update to #42. `skip:` keeps a message and its reply out of every agent run. "Skip button is broken" has no colon, so it is an ordinary bug report. The bot's own confirmation reaction is ignored. |
| `mentions.json` | A bug report with `<!here>` and `<@U0BOB>`, and Bob's reply taking it: "assign it to me". The agent picks `acme-co/api` by its description and assigns `acme-bob`, Bob's linked GitHub login. Alice is mentioned in the reply but not assigned. |
| `mention-without-ownership.json` | An idea that mentions Bob, and Bob's reply with an opinion, but nobody says who takes it. One topic and one `create_issue` proposal with no assignees, and one `ask_channel` question asking who should own it. |

Each file repeats its outcome in `description` and `expected.summary`.

**Mentions never assign.** The two mention fixtures pin the rule on #23: a mention marks someone as involved, never as an assignee. An assignee is proposed only when someone says so in words, as Bob does in `mentions.json`. When ownership is still unclear after the thread settles, the agent asks once in the thread. `mention-without-ownership.json` models that ask, rather than waiting silently, because the rule prefers asking to leaving the assignee blank without a word.

## Format

A file is one JSON object. Unknown fields are rejected everywhere, so a typo fails loudly.

| Field | Holds |
|---|---|
| `format` | Always `"jotflow-thread/1"`. A later version may add fields. |
| `name` | The file name without `.json`. |
| `description` | What the thread is and what should happen. |
| `workspace` | `team`, `team_id`, and the `bot`'s `user_id`, `bot_id`, and handle `name`. |
| `users` | Slack user id to `name`, an optional linked `github` login, and `guest` and `external` flags. |
| `policy` | The same structure as `jotflow.toml`: channels, their repos, and label allowlists. |
| `existing_topics` | Topics that exist before the thread starts, as `jotflow.models.Topic`. |
| `events` | Slack events in delivery order. See below. |
| `agent` | The scripted fake agent: one entry per drafting run. See below. |
| `expected` | The outcome. See below. |

### Events

Only the fields JotFlow reads are allowed.

- A `message` has `channel`, `ts`, and optionally `thread_ts`, `user`, `bot_id`, `subtype`, and `text`. A reply's `thread_ts` names an earlier top-level message. Mentions use Slack's `<@U0BOB>` form, and every mentioned user must be listed in `users`.
- A `reaction_added` has `user`, `reaction`, and an `item` with `type: "message"`, `channel`, and `ts` of an earlier message.
- Two identical events are a redelivery, which ingest must drop. Two different messages may not share a `ts`.

### Agent script

Each run has the `channel` and top-level `thread_ts` it drafts, and the `calls` it makes in order. Every call has a `tool` and `arguments`, validated by the same models as the real MCP tools:

| `tool` | `arguments` |
|---|---|
| `link_message` | `jotflow.models.Classification` |
| `propose_issue` | `jotflow.models.IssueProposal` |
| `propose_update` | `jotflow.models.UpdateProposal` |
| `ask_channel` | `{"text": ...}` |

Real topic ids are assigned by JotFlow at replay time, so a `link_message` that starts a new topic also carries a `topic_key`, and later calls refer to that topic as `key:<topic_key>`. A proposal always names its topic, either `key:<name>` or an existing topic id. Proposals must target a repo the channel lists, use labels from its allowlist, and assign only linked GitHub logins.

### Expected outcome

| Field | Holds |
|---|---|
| `summary` | One sentence. |
| `accepted` | Events ingest accepts, in order: a message `ts`, or `ts:reaction` for a reaction. |
| `ignored` | Events ingest drops, as `ts` or `ts:reaction`, mapped to the reason it gives. |
| `topics` | Each topic, as `key:<name>` or an existing id, with the `ts` of its messages. |
| `chatter` | Messages the agent marks as chatter. |
| `keywords` | Messages that start with a keyword, mapped to `idea`, `bug`, `re`, or `skip`. |
| `proposals` | Each proposal's `kind`, `repo`, `topic`, and for updates `issue_number`. |
| `asks` | The thread `ts` of each `ask_channel` call, in order. |

The loader checks that the outcome agrees with itself and with the script. Every accepted message must be in exactly one topic, in chatter, or in a skipped thread. The script must create exactly the listed topics, chatter, proposals, and asks. Messages attached by `re:` are JotFlow's doing, so they appear in `topics` without a `link_message` call.

## Using them

```python
from jotflow.testing.threads import builtin_thread, load_thread

thread = builtin_thread("bug-report")
thread = load_thread("path/to/saved.json")
```

`load_thread` raises `ThreadError` with one line per problem, each naming the field at fault.
