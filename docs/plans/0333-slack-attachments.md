# Slack attachments in drafting and in the issue

## Links and status

- Issue: #333
- Design: agreed with security on the issue, 2026-10-29, with five conditions
- Status: in progress by the architect
- Next safe action: review the PR against the design and conditions below

## Objective and done-when

A post's attached file, such as a `.md` naming standard, reaches drafting, and a small text file's contents can be included in the issue, exactly as the card shows.

From #333:

- [ ] A design is agreed with security.
- [ ] Drafting sees each attachment's name, type and size, and the text of small text files within the agreed limits.
- [ ] The issue can carry or reference the attachment under the agreed rules, and the card says what will be included.
- [ ] Tests and a replay fixture cover it.

## Design

The design is on #333. In short:

- **Setting:** `[channels.<id>] attachments = "off" | "names" | "text"`. The default is `names`. `text` needs the bot's `files:read` scope.
- **Packet:** each message's files get an opaque `file_id`, a cleaned untrusted `name`, `filetype`, `mimetype` and `size`, never a URL. With `text`, a small allowlisted text file also gets its untrusted `text`. Anything else gets a fixed `not_read` reason.
- **Fetch:** JotFlow's orchestrator does it, never an agent or a runner.
  - It takes only a `url_private` on `https://files.slack.com` from Slack's own data, with the bot token in the Authorization header.
  - It follows no redirects and takes no proxy from the environment.
  - It reads at most 32 KiB a file and 64 KiB a thread.
  - The text must be strict UTF-8 with no NUL bytes, and invisible characters are removed.
- **Propose:** `propose_issue` takes `attachments: [file_id]`, at most 3, each one the run's packet served with text. JotFlow reads each file again and snapshots its name, size, sha256 and text into the draft, so the hash locks it.
- **Refusals,** each with a fixed reason:
  - a file in a channel that doesn't share it;
  - a file whose text looks like it holds a credential;
  - under `issue_links = "none"`, a file holding a Slack link;
  - a body that, with its attachments, would pass GitHub's length limit.
- **Card:** "Includes <name>, <size>, as a code block", then the full text as plain text, under the card's length rule.
- **Execution:** before writing, JotFlow checks through `files.info` that each file still exists, is still shared in the channel, and hashes to the snapshot. Any that doesn't is left out, and the thread notice says so. The body gains a JotFlow-built `## Attachments` section: each file under its cleaned name, in a fenced block one backtick longer than the longest run in its text.
- **Retention:** clearing a draft clears its attachments. Nothing else stores file text.

## Security's conditions (2026-10-29)

1. Scan for credential shapes before proposing or snapshotting, and refuse on a match.
2. A deletion or change after approval wins at execution.
3. A strict fetch: the exact host, `url_private` from Slack's data, no redirects, and a mimetype on the allowlist.
4. Retention: clearing a draft clears its text.
5. Docs: `files:read` reaches every file in the bot's channels, and the threat model gains rows for instructions and secrets in a file.

## Progress checklist

- [ ] Policy setting and writer
- [ ] File reader with the strict fetch and the secret scan
- [ ] Packet files
- [ ] propose_issue snapshot and refusals
- [ ] Card lines
- [ ] Execution check and the Attachments section, and the thread notice
- [ ] Cleared drafts
- [ ] Docs, threat model, CHANGELOG, and a replay fixture
