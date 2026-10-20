# Using JotFlow

How JotFlow works for the people in a channel, and how an admin can query it from a terminal. Setting it up is in [install.md](install.md).

This is how JotFlow works for the people in the channel.

- **Just post.** Write your idea, bug, or request in the channel like you normally would. JotFlow waits for the conversation to settle, then posts a proposed issue for the team to approve.
- **Steer it if you like.** Start a message with `idea:` or `bug:` to say what it is, `re:` to add to an existing issue (JotFlow's link replies tell you exactly what to type), or `skip:` if it isn't an idea at all. Reacting with :no_entry_sign: works like `skip:`. Most people never need these.
- **See where it went.** Once an issue is written, JotFlow replies in the thread with its link and adds a :ticket: reaction to the messages it came from. Set `issue_reaction` in `jotflow.toml` to use another emoji, or `""` for none.
- **Change how a channel works.** `/jotflow mode` shows the channel's mode: `propose`, the default, where nothing is written until someone approves; `auto`, where proposals are written straight away; or `paused`, where JotFlow reads nothing, starts nothing new, and posts nothing; an execution run already running when the channel is paused may still finish its GitHub write; approved runs not yet started wait until it's unpaused. `/jotflow mode auto` sets it, and JotFlow announces the change in the channel. With approvers on, only they can change it.
- **React instead.** :no_entry_sign: skips a message, and :bulb: or :bug: says it is an idea or a bug. Remove your reaction to take it back. If your Slack app was created before this release, update it from the new manifest and reinstall it, as [manifests/README.md](../manifests/README.md) explains, or removing a reaction does nothing.
- **Link your GitHub login.** `/jotflow link your-github-login` lets proposals assign you, and `/jotflow unlink` removes it. Links are self-declared, so cards mark them "(unverified)". If someone linked a login that's yours, an approver, or an admin listed under `[identity]` in `jotflow.toml`, can remove it with `/jotflow unlink` and that person's Slack member ID. Copy it from their profile's ⋯ menu; names aren't accepted. The person is told privately. Whoever runs JotFlow can do the same on the host with `jotflow identity unlink <member-id>`.
- **Forgot the words?** Type `/jotflow help`. Only you see the answer, and it lists this channel's repos and how approval works here.
- **Issues follow your house rules.** A drafted issue copies the title format and headings of the repo's recent issues. For rules those can't show, an admin can give a repo `conventions` in `jotflow.toml`, plain text the agent follows first. A `title_pattern` makes a title format a hard rule: JotFlow refuses a proposed title that doesn't match, and the agent tries again. examples/jotflow.example.toml shows both.
- **Help finds you.** JotFlow pins a short guide once in each channel. Your first message gets a one-time private tip, and a message that looks like a mistyped keyword, such as `skip` or `Idea - ...` without the colon, gets a private suggestion once. JotFlow never acts on a near miss. Each of these can be turned off in `jotflow.toml`'s `[help]` table, for every channel or for one.

The keywords, exactly as `/jotflow help` shows them:

<!-- jotflow-help:start -->
```
Start a message with a keyword to steer JotFlow. The colon is required.
  idea:       this is an idea; the agent takes it as a hint
  bug:        this is a bug; the agent takes it as a hint
  re: web#42  add this to an existing issue; re: 42 works when the channel has one repo
  skip:       not an idea; on a top-level message it keeps the whole thread out
Or react instead: :no_entry_sign: for skip:, :bulb: for idea:, or :bug: for bug:. Remove your reaction to take it back.
/jotflow link your-github-login lets proposals assign you; /jotflow unlink undoes it.
Approvers can remove someone else's link with /jotflow unlink and their Slack member ID.
/jotflow repos lists the repos this channel feeds.
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
