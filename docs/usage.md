# Using JotFlow

How JotFlow works for the people in a channel, and how an admin can query it from a terminal. Setting it up is in [install.md](install.md).

This is how JotFlow works for the people in the channel.

- **Just post.** Write your idea, bug, or request in the channel like you normally would. JotFlow waits for the conversation to settle, then posts a proposed issue for the team to approve.
- **Steer it if you like.** Start a message with `idea:` or `bug:` to say what it is, `re:` to add to an existing issue (JotFlow's link replies tell you exactly what to type), or `skip:` if it isn't an idea at all. Reacting with :no_entry_sign: works like `skip:`. Most people never need these.
- **Keep adding to it.** While a card waits for approval, new replies about it revise the same card. It says whose messages changed it and what changed, so reread it before approving. Once the issue is filed, a follow-up becomes a proposed comment on it, or a change to its description when the request itself changes, such as its scope. A new post that clearly continues a filed topic does the same, and when it's unclear which topic it continues, the agent asks. A quick second post of your own also counts: "And the east screen." a few seconds after your idea is read as adding to it, and "correction: ..." as correcting it, though nothing stops it being a new idea if it is one (#334).
- **Attach a file.** Files on a message reach the agent by name, type and size. If an admin sets a channel's `attachments = "text"`, a small text file's contents reach it too, up to 32 KiB, and the agent can include the file in the issue. The card then shows "Includes oven-names.md" with the file's full text, and the issue gets that text as a code block, exactly as shown. A file that looks like it holds a password or token is never read, and one you delete or change before the issue is filed is left out (#333).
- **Put it on a board.** If a channel lists GitHub Projects boards in `jotflow.toml`, the agent may propose up to three for a new issue, choosing by each board's description, and the card shows them with its reason for each. Before approving, anyone who may edit the card can change its boards from the card's menu, choosing up to three. Approving adds the issue to those boards, and the thread reply says which. Only organisation boards work.
- **See where it went.** Once an issue is written, JotFlow replies in the thread with its link and adds a :ticket: reaction to the messages it came from. Set `issue_reaction` in `jotflow.toml` to use another emoji, or `""` for none.
- **Catch a duplicate.** When the agent found issues close to the idea, the card lists them under **Related**, with whether the agent says each is open or closed. When the agent thinks one may be the same request, it says "maybe the same request" beside it, and the card offers **Same as** for it. A merely related issue gets no button (#337). If it is the same idea, click **Same as**: nothing new is written, and later messages in the thread go to that issue. Anyone who may approve can click it. **Not a duplicate** takes it back, and JotFlow drafts the thread again.
- **Change how a channel works.** `/jotflow mode` shows the channel's mode: `propose`, the default, where nothing is written until someone approves; `auto`, where proposals are written straight away; or `paused`, where JotFlow reads nothing, starts nothing new, and posts nothing; an execution run already running when the channel is paused may still finish its GitHub write; approved runs not yet started wait until it's unpaused. `/jotflow mode auto` sets it, and JotFlow announces the change in the channel. With approvers on, only they can change it.
- **React instead.** :no_entry_sign: skips a message, and :bulb: or :bug: says it is an idea or a bug. Remove your reaction to take it back. If your Slack app was created before this release, update it from the new manifest and reinstall it, as [manifests/README.md](../manifests/README.md) explains, or removing a reaction does nothing.
- **Link your GitHub login.** `/jotflow link your-github-login` lets proposals assign you, and `/jotflow unlink` removes it. Links are self-declared, so cards mark them "(unverified)". If someone linked a login that's yours, an approver, or an admin listed under `[identity]` in `jotflow.toml`, can remove it with `/jotflow unlink` and that person's Slack member ID. Copy it from their profile's ⋯ menu; names aren't accepted. The person is told privately. Whoever runs JotFlow can do the same on the host with `jotflow identity unlink <member-id>`. They can also load a whole team's links at once with `jotflow identity link --file links.csv`, a file with a `slack_member_id,github_login` header, or link one pair with `jotflow identity link <member-id> <login>` (#305). Each login is looked up on GitHub first, through the drafting runner, and nothing is written if any line is wrong or names an organization. Cards show these links as "(linked by an admin)". A login someone else has is refused, unless `--replace` is passed: then their link moves, and they're told privately. Someone who already has a link keeps it as it is if it's the same login and already verified. If it's a different login, the line is refused without `--replace`, and with it they're told, naming the old and new login. A login that now names a different GitHub account than the one stored is always refused. Each line is one change, and anyone it affects is told right after it. A run that stops partway names the member it stopped at, so you can tell them, and can be run again. To drop the "(unverified)" mark, type `/jotflow verify`: JotFlow gives you a one-time code to put in your GitHub profile bio or a public gist's description, then `/jotflow verify check` checks it. If someone else linked your login first, `/jotflow verify your-github-login` lets you prove it's yours, and the link moves to you. GitHub limits how often its public profiles can be read, so at busy times a check may ask you to try again in a few minutes.
- **Ask to be assigned.** Say so in the thread, such as "add me" or "I'll help with this", and the agent adds you as an assignee if your GitHub login is linked: on the waiting card, or as an update once the issue is filed (#305). The card says why each person is assigned: "volunteered in the thread", "named as the owner in the thread", "listed as an owner of this repo" for a repo's `owner_hints`, or "added on the card". The agent assigns nobody when it's unsure, and a mention alone never assigns anyone.
- **Forgot the words?** Type `/jotflow help`. Only you see the answer, and it lists this channel's repos and how approval works here.
- **Issues follow your house rules.** A drafted issue copies the title format and headings of the repo's recent issues. For rules those can't show, an admin can give a repo `conventions` in `jotflow.toml`, plain text the agent follows first. A `title_pattern` makes a title format a hard rule: JotFlow refuses a proposed title that doesn't match, and the agent tries again. It binds only the agent's drafted titles. A person who edits the title on the card isn't held to it (#160). examples/jotflow.example.toml shows both.
- **Tell the agent how your team triages.** Repo and board descriptions are one line each. For anything longer, such as "billing emails go to the API repo, even when customers notice them on the dashboard", an admin can give a channel a `triage_guide` in `jotflow.toml`, of up to 8,000 characters. The agent chooses the repo and boards by it, but only among the listed ones, and never over JotFlow's own rules. `jotflow check` shows its size (#317).
- **Every issue from Slack can carry a label and a title prefix.** In `jotflow.toml`, `[issues] label = "from-slack"` adds that label to every new issue JotFlow files, and `title_prefix = "[Slack]"` puts that text and a space before every new title (#306). Both are off unless set. The card shows them, marking the label "(added to every issue)", and nobody can take them off there. The label should already exist in each repo, since JotFlow never creates labels. In a repo GitHub says doesn't have it, the issue is filed without it, and the thread says "Filed without the label from-slack, which doesn't exist in acme-co/web". If GitHub can't be asked just then, the write stops as for any missing label, and Retry tries again. What's approved is what the card showed: if `[issues]` changes in `jotflow.toml` after a card was drawn, Approve redraws the card instead of approving, and an approved issue is filed with the settings it was approved with, even after a restart. The prefix goes on after a repo's `title_pattern` checks the title, so a strict pattern won't match the filed title. `jotflow check` notes both.
- **A weekly summary of the channel's ideas.** With `[channels.<id>.summary]` in `jotflow.toml`, a channel gets one post a week at its own `day`, `time` and `timezone`, listing the ideas of the past seven days (#308). Each shows its title, who suggested it and who added to it, the day, and its status: waiting for approval, being filed, filed as `web#42`, or couldn't be filed. Buttons open its thread, its card while it waits, and its issue once filed. **Not this week** puts a waiting idea off to the next summary, under Approve's rules for who may click. An idea whose card was rejected, whose draft was removed, or whose messages were all deleted isn't shown again. A paused channel gets no summary that week, and one missed by more than 12 hours, while JotFlow was down, is skipped rather than posted late. At most 20 ideas are listed, waiting ones first.
- **Feed a whole organisation.** Instead of listing every repo, an admin can give a channel a scope in `jotflow.toml`: an owner, name patterns such as `service-*`, and patterns to leave out. Patterns ignore case, as GitHub's repo names do. JotFlow asks GitHub for that owner's repos every 15 minutes, so a new or renamed repo needs no restart, and it leaves out forks and archived repos unless the scope says otherwise. Listed repos still come first, and their settings win. `/jotflow repos` shows both, with a line for each scope and when it was last listed, and `/jotflow repos <word>` narrows a long list. A found repo's card warns that it's public unless GitHub says it's private. Before approving one, JotFlow checks that its write token can reach the repo. examples/jotflow.example.toml shows a scope (#236).
- **Ask JotFlow.** Start a message with `ask:`, or mention the bot, to ask whether something is already tracked in this channel, or how to use JotFlow: "ask: is anyone already on the CSV export?". The agent answers once in the thread, in a few sentences, labelled "Answer from the agent:". It answers from the thread, this channel's topics and the help text. It can also look up an issue in this channel's repos, so it can say whether `owner/name#42` is open or closed, how it's labelled, and when it last changed (#273). It sees an issue's title, but never its body or comments, and it can't search GitHub or read another channel's repos. If GitHub can't be asked right now, the answer says so rather than guessing. The answer names issues as `owner/name#42` for this channel's repos only, and holds no links and pings nobody. A question is never drafted into an issue. Only this workspace's own members can ask: guests and people from other workspaces get a private note instead. Mentioning the bot in a channel JotFlow doesn't watch does nothing. A channel answers up to 10 questions an hour, and one person can ask once a minute, unless `jotflow.toml`'s `[questions]` table says otherwise. `[questions] enabled = false` turns questions off.
- **Help finds you.** JotFlow pins a short guide once in each channel. Your first message gets a one-time private tip, and a message that looks like a mistyped keyword, such as `skip` or `Idea - ...` without the colon, gets a private suggestion once. JotFlow never acts on a near miss. Each of these can be turned off in `jotflow.toml`'s `[help]` table, for every channel or for one.

The keywords, exactly as `/jotflow help` shows them:

<!-- jotflow-help:start -->
```
Start a message with a keyword to steer JotFlow. The colon is required.
  idea:       this is an idea; the agent takes it as a hint
  bug:        this is a bug; the agent takes it as a hint
  re: web#42  add this to an existing issue; re: 42 works when the channel has one repo
  skip:       not an idea; on a top-level message it keeps the whole thread out
  ask:        a question for JotFlow; the agent answers in the thread (#57)
Or react instead: :no_entry_sign: for skip:, :bulb: for idea:, or :bug: for bug:. Remove your reaction to take it back.
/jotflow link your-github-login lets proposals assign you; /jotflow unlink undoes it.
Approvers can remove someone else's link with /jotflow unlink and their Slack member ID.
/jotflow verify proves your GitHub login is yours, so cards stop marking it unverified.
/jotflow repos lists the repos this channel feeds; add a word to narrow it, or change to see how to edit them.
Most people never need these: just post, and JotFlow works out the rest.
```
<!-- jotflow-help:end -->

Comments and closures on GitHub do not flow back into Slack yet; that is planned after v1.

## Ask from a terminal (admins)

An admin can give someone a read-only token, so their own terminal agent can look up a channel's topics. It can call only `list_topics` and `get_topic`, on the channels you name, and it can't propose, write or approve anything.

```sh
docker compose exec jotflow jotflow operator create --name alex --channel C0123ABCD
docker compose exec jotflow jotflow operator list
docker compose exec jotflow jotflow operator revoke alex
```

`create` prints the token once, so pass it on privately. It lasts 30 days unless you add `--days`, up to 365. Repeat `--channel` for more channels. `list` shows each token's channels, expiry and last use, never the token. A revoked token stops working within 2 seconds. A new token works at once, but while JotFlow is getting a flood of bad tokens it can take up to 2 seconds.

No port is opened for this. The person's MCP client starts JotFlow's bridge inside the container as its server command, with the token in its environment:

```json
{
  "mcpServers": {
    "jotflow": {
      "command": "docker",
      "args": ["compose", "--project-directory", "/path/to/jotflow", "exec", "-T",
               "-e", "JOTFLOW_OPERATOR_TOKEN", "jotflow", "jotflow", "operator", "connect"],
      "env": { "JOTFLOW_OPERATOR_TOKEN": "jfo_..." }
    }
  }
}
```

`-e JOTFLOW_OPERATOR_TOKEN` with no value copies the token from the environment, so it never appears in a command line. Keep a config file that holds the token private. From another machine, run the same command over ssh, reading the token from a private file on the host:

```sh
ssh jotflow-host 'cd /path/to/jotflow && JOTFLOW_OPERATOR_TOKEN=$(cat ~/.jotflow-operator-token) docker compose exec -T -e JOTFLOW_OPERATOR_TOKEN jotflow jotflow operator connect'
```

Anyone who can run `docker compose exec` on the host can already read JotFlow's database and Slack tokens, so only give host access to people you would trust with those. JotFlow logs each call with the token's id, the tool, the channel and the time.
