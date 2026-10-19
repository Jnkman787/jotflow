# Changelog

What changed in each JotFlow release, for the people who run it. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change configuration or behaviour, and these notes say when.

## [0.1.0] - unreleased

The first release. JotFlow watches the Slack channels you choose, hands each quiet thread to your team's own agent, and posts what the agent proposes as a card. Nothing reaches GitHub until a person approves it, unless a channel is set to `auto`.

### Install and setup

- **Install without cloning.** `uvx jotflow init`, then `docker compose up -d`, from an empty folder. Setup writes `.env`, `jotflow.toml`, and two Compose files pinned to this release's images on GitHub's container registry.
- **Guided setup.**
  - Setup creates the Slack app from a link with the manifest filled in, checks both tokens with Slack, and asks which channels to watch and which repos each one feeds. Every question explains itself first.
  - With the GitHub CLI signed in, setup lists repos to pick from.
  - It writes the agent containers' files, each with its own runner secret. `--rotate-runner-secret` and `--rotate-exec-secret` replace them.
- **Published artifacts.** The package is on PyPI, and the `jotflow` and `jotflow-agent` images are on GHCR for amd64 and arm64. All are built from the same tag and version, with signed build provenance.

### In Slack

- **Just post.** JotFlow waits for a thread to go quiet, so a burst of replies is read together, then drafts from the whole thread. The wait survives a restart.
- **Keywords**, typed at the start of a message with a colon:
  - `idea:` and `bug:` are hints the agent follows, and the card says when it overrules one.
  - `re: web#42` adds the message to an existing issue.
  - `skip:` keeps a message, or a whole thread, out of drafting.
  - "Skip button is broken" is never read as a keyword.
- **Reactions.** :no_entry_sign:, :bulb:, and :bug: work like `skip:`, `idea:`, and `bug:`. Removing the reaction takes it back.
- **The card.** Each proposal appears as one card in its thread, edited in place as it moves along. It shows the full issue body, the repo, labels, assignees, size, and confidence. It warns about a public repo. Its repo menu and assign buttons edit the proposal before it's approved.
- **Approval.** Approve, Request Changes, Retry, and Discard on the card, or an optional approval reaction. An approval locks exactly what the card showed. Guests and external Slack Connect users never approve. With approvers set, only they can, and never for a proposal drawn from their own messages.
- **Channel modes.** `/jotflow mode` shows or sets `propose`, `auto`, or `paused`. Every change is announced, and `auto` warns in a channel with guests or external members.
- **After the write,** JotFlow replies in the thread with the issue's link and reacts on the messages it came from.
- **Help where people are.** `/jotflow help` explains everything for that channel, privately. JotFlow pins a short guide once per channel, gives a person's first message a one-time tip, and privately suggests the right form for a mistyped keyword. Each of these can be turned off in `[help]`.
- **Linking accounts.** `/jotflow link your-github-login` lets proposals assign you, and `/jotflow repos` lists the repos a channel feeds.

### The agent

- **Your agent, in its own containers.** Drafting and questions run in one container with a read-only GitHub token. Approved writes run in another, with the token that can write. The write token never sits where untrusted Slack text is read.
- **Presets.** `claude-code` starts Claude Code locked down: only JotFlow's tools and the GitHub MCP server, no shell, file, or web tools, nothing persisted, and a model you can choose per run kind. `command` runs your own agent instead.
- **Exact writes.** An execution run gets only the approved fields. It finds an issue it already wrote by a hidden marker before writing again, and JotFlow checks the reported issue URL against the approved repo. A failed write is retried a few times, then reported on the card.
- **Issue bodies** carry permalinks to the Slack messages they came from, or no Slack details at all with `issue_links = "none"`.

### Running it

- **Health.** `docker compose ps` shows `healthy` while JotFlow is connected to Slack, and logs say when the connection drops and comes back.
- **Operator tokens.** `jotflow operator create` issues an admin's read-only token for chosen channels, and `jotflow operator connect` bridges it to an MCP client over stdio. No port is published.
- **Replay.** `jotflow replay` runs a saved thread through the real pipeline with no Slack or network, to see what JotFlow would do. `jotflow check --live` confirms a real Slack app hears its channel.
- **Docs.** The [deployment guide](docs/deployment.md) covers servers, backups, upgrades, and rollback. The [threat model](docs/threat-model.md) says what JotFlow protects and what it accepts.

### Security

- **No GitHub credentials in JotFlow.** The team's own tokens live only in the agent containers.
- **Thread text** is never stored. Drafts are removed once a proposal is final, and the database overwrites what it deletes.
- **Logs** never hold message text, and every token shape JotFlow knows is redacted.
- **Agent runs** get a short-lived token scoped to one run, one channel, and one thread. The containers drop all capabilities, run read-only as their own user, and publish no port.

### Known limitations

The threat model's [Accepted in v1](docs/threat-model.md#accepted-in-v1) lists each deliberate gap, why it's accepted, and its follow-up. In short:

- GitHub logins linked with `/jotflow link` are self-declared until verification exists. Cards mark them "(unverified)".
- Approval is open to anyone in the channel by default, including the poster. Turn approvers on for wide or external membership.
- With `issue_links = "none"`, names typed into a message can't be detected mechanically. A person reads the full body before approving.
- The Codex preset is experimental and not a default.
- Runs of the same kind can read each other's environment. They already hold the same token.
- A custom agent command's tools are the team's responsibility.
- Rebuilding the database from Slack isn't built yet, so keep backups.

[0.1.0]: https://github.com/Jnkman787/jotflow/releases/tag/v0.1.0
