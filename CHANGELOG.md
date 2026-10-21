# Changelog

What changed in each JotFlow release, for the people who run it. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change configuration or behaviour, and these notes say when.

## [Unreleased]

### Added

- **`jotflow replay` plays question runs (#270).** Fixtures can script a question run's answer and expect what JotFlow posts after its no-links rewrite. Four new fixtures cover a question about an existing topic, a how-to question, an injected link-and-ping request, and a guest who is refused privately.
- **A question can check an issue's status (#273).** The agent can look up an issue in the asking channel's repos, so an answer can say whether `owner/name#42` is open or closed, why it was closed, how it's labelled, and when it last changed. It reads through the drafting runner with the read token, at most 5 issues a question, and never sees an issue's body or comments. Another channel's repos are refused. A rate limit makes the answer say GitHub couldn't be checked, never that the issue doesn't exist. The question run itself still has no GitHub access. Update both images: the drafting runner gains one fixed GitHub call.
- **`jotflow rebuild --from-github` (#256).** It's optional. It reads JotFlow's own issues through the agent runners, so JotFlow still holds no GitHub token. It restores real topic titles, and threads whose Slack reply was lost. Only issues the write token's own account wrote count, and only messages the Slack walk saw are linked, at the agent's level.

### Changed

- **The codex preset checks each model's tools at startup, instead of accepting only `gpt-5.5` (#220).** The runner runs Codex once per configured model against a local fake API and refuses to start if Codex would offer a code-execution tool or a sub-agent namespace, naming them. `gpt-5.5` stays the default and passes. Another model now works if it passes, so set `JOTFLOW_AGENT_MODEL` by hand to try one. Each check takes about a second and calls no real model.
- **`read_paths` works with the codex preset (#274).** Codex's MCP resource tools can't be turned off, but the GitHub server it starts serves no repository content behind them. The runner checks that at every start and won't start otherwise. `jotflow check` no longer fails the pair, and drafting stays on.
- **`jotflow rebuild` restores a thread's links as the agent's decision, not a person's (#279).** The original links were mostly the agent's, so a later drafting run can still move a rebuilt one, and a person's later choice wins. A `skip:` stays a person's. A message a person attached before the loss, for example with `re:`, comes back as the agent's too, so attach it again with `re:` to keep it fixed.

### Security

- **The codex preset no longer fetches OpenAI's model catalog (#284).** With an API key, Codex fetched it on every run, and that catalog sets each model's tools. A change on OpenAI's side could have switched `gpt-5.5` into Codex's code mode, which adds a JavaScript `exec` tool, and JotFlow's model check wouldn't have caught it. Codex now uses the catalog bundled with the pinned release. Only teams on the `codex` preset are affected. Upgrade the agent image, which builds the config.

## [0.4.0] - 2026-10-20

A minor release with new features, all off by default except the readable help, related issues on the card, and the @-mention check. Upgrade both images: the agent image changed too. The default Compose files are unchanged, so an image bump is enough, and `docker-compose.isolation.yml` is new and only for teams turning on per-run uids. It adds store migrations 14 and 15, so rolling back means restoring the backup from before the upgrade, then removing any new keys from `jotflow.toml` (`scopes`, `read_paths`, `conventions`, `title_pattern`, `[questions]` and any `[channels.<id>.questions]`), which older versions refuse. No Slack app change is needed.

### Added

- **Drafting can read a repo's design docs, opt-in (#209).**
  - A repo in `jotflow.toml` can set `read_paths`, such as `["README.md", "docs/plans/**/*.md"]`. Drafting runs then get `list_repo_docs` and `read_repo_doc`, which read only the text files those patterns allow, through the drafting agent container.
  - Reads are capped at 64 KiB a file, and 20 files and 256 KiB a run. The text is marked untrusted, and the card lists every doc the draft read and flags a long passage copied word for word.
  - The read token then needs **Contents: Read-only**, which GitHub grants for every file in every repo the token reaches, not only these paths. `jotflow check` says so.
  - The `codex` preset can't be used with `read_paths`, because its MCP resource tools would read past it. `jotflow check` fails, and JotFlow turns drafting runs off with a reason naming the conflict; question runs, which have no GitHub access, stay on (#274).
  - With no `read_paths`, nothing changes. Before rolling back, remove `read_paths`, because older versions refuse keys they don't know.
- **Prove a GitHub link is yours (#195).** `/jotflow verify` gives you a one-time code to put in your GitHub profile bio or a public gist's description, and `/jotflow verify check` checks it. A verified login loses "(unverified)" on cards. If someone else linked your login first, `/jotflow verify your-github-login` lets you prove it and moves the link to you; they're told privately. The check runs in the drafting agent container, anonymously, so JotFlow itself still never talks to GitHub, and it needs the agent containers on. GitHub limits anonymous reads, so a busy host may be asked to try again later. This adds migration 15.
- **Per-run uids in the agent containers, opt-in (#181).** With `JOTFLOW_RUNNER_ISOLATION=uid` in an agent env file and `docker-compose.isolation.yml` in `COMPOSE_FILE`, each run's agent runs as its own Linux uid with no capabilities. So runs of one kind can't read each other's environment, run directory or MCP config, or the runner's, which holds the GitHub token and runner secret. The runner starts as root with only SETUID and SETGID, and refuses to start without them. This mode needs an API key, not a CLI login. The default, `none`, is unchanged. Making `uid` the default is planned for a later minor release.
- **Questions with `ask:` or an @-mention of the bot (#57).** The agent answers once, in the thread, from the channel's topics and from `/jotflow help`. A question run has no GitHub access. The answer is plain text labelled "Answer from the agent:", with no links, no pings, and no other repos' references. Only this workspace's own members can ask. A new `[questions]` table sets a per-channel hourly limit (10) and a per-person wait (60 seconds), or turns questions off. No new Slack scope is needed. Plain file names like `Node.js` and `config.yaml` stay readable, while anything Slack could link, such as `README.md`, is removed (#275).
- **Per-repo house rules for issues (#216).**
  - A repo in `jotflow.toml` can have `conventions`, the team's own rules for its issues, which the drafting agent follows over the style of recent issues.
  - A repo can also have a `title_pattern` that every proposed title must fully match. JotFlow refuses a title that doesn't, and the agent tries again, up to three times per run.
  - Each match runs under a 50 ms limit, off the event loop. A title that can't be checked in time is refused, never accepted, and `jotflow check` warns about a pattern that slow.
  - Setup keeps both settings when it rewrites `jotflow.toml`.
  - Before rolling back to 0.3.0 or earlier, remove both settings, because older versions refuse keys they don't know.
- **`jotflow rebuild`** restores topics, message links, channel modes and the guide flag from JotFlow's own Slack replies, after the database is lost without a backup. It stores pointers only, never message text, and doesn't touch GitHub. [docs/deployment.md](docs/deployment.md#rebuild-from-slack-without-a-backup) lists what it can't restore (#38).
- **Related issues on the card, and Same as (#36).** The card lists up to three issues the agent found close to the idea, marking each open or closed as the agent's claim. **Same as** links the conversation to the first one instead of writing a new issue, under Approve's rules for who may click. **Not a duplicate** takes it back and drafts again. The store gains migration 14, with pointers and Slack ids only.

- **A channel can feed every repo an owner has, or those matching a pattern (#236).** A channel's `[[channels.<id>.scopes]]` names an owner, `match` and `exclude` patterns that ignore case, and optional labels, aliases and a description. JotFlow lists the owner's repos through the drafting runner every 15 minutes, so a new or renamed repo needs no restart. Forks and archived repos are left out unless the scope says otherwise. Approval checks a found repo again, and refuses one the write token can't see, saying how to fix it. Both runners gain one fixed GitHub call each, so update the agent images with JotFlow's. Before rolling back, remove every scope, because older versions refuse the key.

### Changed

- **`/jotflow repos <word>` now filters the list (#236).** It shows the repos whose nickname, name, description or topics contain the word. How to change the repos moved to `/jotflow repos change`, and `/jotflow repos add …` and `remove …` still show it. Before, any word after `/jotflow repos` showed that text.
- **`/jotflow help` and `/jotflow repos` are easier to read (#257).** Each reply has short headed sections, for keywords, reactions, repos, approval and commands. Repos are a bulleted list with the nickname and `owner/name` in code. They still can't ping or link: Slack is told to show the text verbatim, and every name and description from `jotflow.toml` is escaped. A long list still fits Slack's limits, and ends "and N more" when it doesn't.

### Security

- **No stray @-mentions in drafted issues (#250).** `propose_issue` and `propose_update` refuse a title, body or comment with an `@login` or `@org/team` that GitHub would ping, unless it's the linked GitHub login of a participant. That covers a login copied from the thread, or from an issue the agent read for style. Code spans and fenced blocks are exempt, and neither invisible characters nor HTML entities such as `&#64;` can hide a mention. The agent gets a fixed reason telling it to write the name without @.

## [0.3.0] - 2026-10-20

A minor release: drafted issues are fuller and follow each repo's own style. Upgrading only needs the new image version. The Compose files, the Slack app and `jotflow.toml` are unchanged since 0.2.0, and rolling back is a plain image swap.

### Changed

- **Drafted issues are fuller, and follow the repo's own style (#249).**
  - **Style:** before proposing, the agent reads the target repo's ten most recent issues and matches their title format, such as `fix(api): subject`, and their section headings. It copies only the form, never their text, links, mentions, or labels, and treats them as untrusted.
  - **Default sections:** when the repo shows no shared style, the body uses `## Problem`, `## Proposal`, `## Suggested approach`, `## Acceptance` as a `- [ ]` checklist, and `## Context`.
  - **The agent's own plan:** it analyses the idea, so even a one-line post becomes a useful issue. Its numbered breakdown is labelled "Drafted by the agent from the discussion; adjust before starting", and Acceptance adds its own completion criteria to what people asked for. Its plan is described in words, with no commands, install steps, new dependencies, links, or secret handling unless the thread gave them. It never states as fact current behaviour, numbers, file paths, or decisions the thread didn't give. Something from a related issue goes under Context only, credited as "per owner/name#N", and nothing from the issues it reads for style goes in at all. Open questions go under Context.
  - **New replay checks** fail any number in Acceptance or Suggested approach, any link in the body, or any command line, that the thread never gave.

## [0.2.0] - 2026-10-20

A minor release, because it changes who may approve in channels with an approver list. Upgrading only needs the new image version: the Compose files and the Slack app are unchanged since 0.1.1. If you roll back to 0.1.1, remove `require_second_approver` from `jotflow.toml` first, because 0.1.1 refuses keys it doesn't know.

### Changed

- **A listed approver may approve their own proposal (#244).** With approvers on, a listed approver can now approve, by button, reaction or Retry, a proposal drawn from their own messages. Until now that was always refused. To keep the old rule, set `[approval] require_second_approver = true`, which needs an approver list. With approvers off, nothing changes: anyone in the channel may approve.

## [0.1.1] - 2026-10-19

A fix release. Upgrade by re-running setup with this version and replacing both Compose files when it asks: the DNS fix is in the Compose files, so changing only the image version in `.env` misses it. If `.env` sets `JOTFLOW_IMAGE_REF` or `JOTFLOW_AGENT_IMAGE_REF`, change both to 0.1.1 or remove them, since they override the Compose files' pins.

### Security

- **Runner secrets and run tokens go only to private addresses (#234).**
  - JotFlow resolves each runner's name once, refuses unless every address is private, loopback or link-local (100.64/10 included, for VPNs such as Tailscale), and connects to the address it checked. The runner refuses a run unless JotFlow's MCP host resolves the same way.
  - The Compose files set `dns_search: ["."]`, so a service that isn't running can't resolve through the host's search domain to a public host.
  - A missing runner now fails its startup check in about 15 seconds, not 2 minutes.
  - `JOTFLOW_RUNNER_ALLOW_PUBLIC` and `JOTFLOW_MCP_ALLOW_PUBLIC` opt in to public addresses.

### Fixed

- **Setup and `jotflow check` catch a model key the runner never passes (#239).** An agent env file kept from before setup ran could set `ANTHROPIC_API_KEY` without listing it in `JOTFLOW_AGENT_ENV_ALLOW`, so every run failed to sign in, and `check` still passed. Setup now adds the key to that line and says so. `check` fails, naming the file and the line to set. A Claude account login with no key is left alone.
- **The "Agent model" prompt shows its default,** as `[default]` or the model already set, like setup's other prompts (#239).
- **`/jotflow help` and `/jotflow repos` fit Slack's limits with many repos or long repo names (#231).** Both now reply in plain-text blocks of at most 3,000 characters, so nothing in them can ping or link. Help lists repos up to a length, then says how many more `/jotflow repos` shows. `/jotflow repos` lists every repo, up to Slack's message limit (#237, #238).

## [0.1.0] - 2026-10-19

The first release. JotFlow watches the Slack channels you choose, hands each quiet thread to your team's own agent, and posts what the agent proposes as a card. Nothing reaches GitHub until a person approves it, unless a channel is set to `auto`.

### Install and setup

- **Install without cloning.** `uvx jotflow init`, then `docker compose up -d`, from an empty folder. Setup writes `.env`, `jotflow.toml`, and two Compose files pinned to this release's images on GitHub's container registry.
- **Guided setup.**
  - Setup creates the Slack app from a link with the manifest filled in, checks both tokens with Slack, and asks which channels to watch and which repos each one feeds. Every question explains itself first.
  - With the GitHub CLI signed in, setup lists repos to pick from.
  - It asks which agent your team runs, `claude-code` or `codex`, or `none` for now. Then it writes the agent containers' files, each with its own runner secret. `--rotate-runner-secret` and `--rotate-exec-secret` replace them.
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
- **Linking accounts.** `/jotflow link your-github-login` lets proposals assign you, and `/jotflow repos` lists the repos a channel feeds. An approver or identity admin can remove someone else's link with `/jotflow unlink <member-id>`, and the person is told privately.

### The agent

- **Your agent, in its own containers.** Drafting and questions run in one container with a read-only GitHub token. Approved writes run in another, with the token that can write. The write token never sits where untrusted Slack text is read.
- **Presets.** `claude-code` starts Claude Code locked down: only JotFlow's tools and the GitHub MCP server, no shell, file, or web tools, nothing persisted, and a model you can choose per run kind. `codex` starts Codex CLI with no shell or web tools and only JotFlow's and GitHub's tools, though it keeps MCP resource-read tools, which reach public repos only, and a patch tool its read-only sandbox refuses. It serves drafting and question runs on `gpt-5.5`, while execution stays on claude-code. The published agent image carries both CLIs. `command` runs your own agent instead.
- **Exact writes.** An execution run gets only the approved fields. It finds an issue it already wrote by a hidden marker before writing again, and JotFlow checks the reported issue URL against the approved repo. A failed write is retried a few times, then reported on the card.
- **Issue bodies** carry permalinks to the Slack messages they came from, or no Slack details at all with `issue_links = "none"`.

### Running it

- **Health.** `docker compose ps` shows `healthy` while JotFlow is connected to Slack, and logs say when the connection drops and comes back.
- **Removing a link from the host.** `jotflow identity unlink <member-id>` removes someone's GitHub link and tells them.
- **Operator tokens.** `jotflow operator create` issues an admin's read-only token for chosen channels, and `jotflow operator connect` bridges it to an MCP client over stdio. No port is published.
- **Replay.** `jotflow replay` runs a saved thread through the real pipeline with no Slack or network, to see what JotFlow would do. `jotflow check --live` confirms a real Slack app hears its channel.
- **Docs.** The [deployment guide](docs/deployment.md) covers servers, backups, upgrades, and rollback. The [threat model](docs/threat-model.md) says what JotFlow protects and what it accepts.

### Security

- **No GitHub credentials in JotFlow.** The team's own tokens live only in the agent containers.
- **Thread text** is never stored. Drafts are removed once a proposal is final, and the database overwrites what it deletes.
- **Logs** never hold message text, and every token shape JotFlow knows is redacted.
- **Agent runs** get a short-lived token scoped to one run, one channel, and one thread, and, for writes, one approved proposal. The containers drop all capabilities, run read-only as their own user, and publish no port.

### Known limitations

The threat model's [Accepted in v1](docs/threat-model.md#accepted-in-v1) lists each deliberate gap, why it's accepted, and its follow-up. In short, with the [After v0.1.0](docs/roadmap.md#milestone-6-after-v010-todo) milestone holding the planned follow-ups:

- GitHub logins linked with `/jotflow link` are self-declared. Cards mark them "(unverified)", and verifying them is planned after 0.1.0 (#195).
- Approval is open to anyone in the channel by default, including the poster. Turn approvers on for wide or external membership.
- With `issue_links = "none"`, names typed into a message can't be detected mechanically. A person reads the full body before approving.
- The Codex preset serves drafting and question runs only, on `gpt-5.5` only. Its tool set is checked in CI, but no real-model pass is recorded yet.
- Runs of the same kind can read each other's environment. They already hold the same token, and per-run isolation is planned after 0.1.0 (#181).
- A custom agent command's tools are the team's responsibility.
- Rebuilding the database from Slack isn't in 0.1.0, so keep backups. It's planned after 0.1.0 (#38).

[Unreleased]: https://github.com/Jnkman787/jotflow/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Jnkman787/jotflow/releases/tag/v0.4.0
[0.3.0]: https://github.com/Jnkman787/jotflow/releases/tag/v0.3.0
[0.2.0]: https://github.com/Jnkman787/jotflow/releases/tag/v0.2.0
[0.1.1]: https://github.com/Jnkman787/jotflow/releases/tag/v0.1.1
[0.1.0]: https://github.com/Jnkman787/jotflow/releases/tag/v0.1.0
