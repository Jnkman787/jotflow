# Install JotFlow

The full setup walkthrough, from an empty folder to a running JotFlow with its agent. The [README](../README.md) has the short version, [troubleshooting.md](troubleshooting.md) covers errors, and [usage.md](usage.md) is for the people in the channel.

**What works today:** With `[agent] preset` set in `jotflow.toml` and the agent containers set up, JotFlow starts an agent run on each thread in the channels you choose, once the thread goes quiet. The agent drafts a proposal, which JotFlow posts as a card for the team to approve, or asks a clarifying question in the thread. An approved card becomes a GitHub issue or comment, written by the agent's execution container, and JotFlow replies in the thread with its link. Without a preset, JotFlow connects and answers `/jotflow`, but drafts nothing.

Setup usually takes ten to fifteen minutes, most of it in Slack's settings pages. Turning the agent on afterwards, with two GitHub tokens and a model login, takes about ten more. It can take longer the first time Docker downloads JotFlow, or if your workspace needs an admin to approve new apps. Every name and id below is made up; use your own.

## Words used here

- **Terminal**: the window where you type commands. On macOS it is called Terminal. On Windows, use PowerShell, or a WSL terminal if you use WSL.
- **Your JotFlow folder**: the folder you make in step 2, where setup writes its files. Run every command below from inside it.
- **Token**: a long secret string Slack gives your app, like a password for it. Setup asks for two.
- **Scope**: one permission a Slack app has, such as reading messages in channels it was invited to.
- **Socket Mode**: how JotFlow talks to Slack. It connects out to Slack, so your machine needs no public address or open ports.
- **`.env`**: a small file that holds the two Slack tokens. Setup writes it; keep it private.
- **Preset**: the name of the agent program your team runs, such as `claude-code`, so JotFlow knows how to start it.
- **MCP**: the standard way JotFlow and your agent talk to each other. You do not need to set it up.
- **`jotflow.toml`**: JotFlow's settings file: which channels to watch and which GitHub repos they feed. It is plain text in the TOML format, and you can edit it.
- **Label**: a tag on a GitHub issue, such as `bug` or `enhancement`.
- **OAuth**: Slack's name for how an app gets its permissions. It appears in page and token names, such as **OAuth & Permissions** and **Bot User OAuth Token**.
- **Handle**: a Slack name with `@` in front, such as `@idea_bot`, that you type to mention someone or something.
- **Manifest**: a file describing a Slack app: its name, permissions, and settings. Slack builds the app from it, so you don't click through every setting.
- **Docker, image, container**: Docker runs programs in a sealed box called a container. The image is what the box is built from. Docker Compose, the `docker compose` command, starts JotFlow's container with the right settings.
- **Slash command**: a command typed in Slack that starts with `/`, such as `/invite` or `/jotflow help`.
- **Milestone**: a stage on the [roadmap](roadmap.md), from "installable" to "ready for another team".

## 1. Prerequisites

- **Docker**, to run JotFlow, with Docker Compose version 2.24 or newer. Docker Desktop includes it: install it from <https://docs.docker.com/get-started/get-docker/>, then start it. On Linux you can use Docker Engine instead: <https://docs.docker.com/engine/install/>. Check with `docker compose version`; a good answer looks like `Docker Compose version v2.29.7`.
  - **On Windows with WSL**, also open Docker Desktop, then **Settings**, **Resources**, **WSL integration**, and switch it on for your Linux distribution, such as Ubuntu. Click **Apply & restart**. Without it, the WSL terminal says `docker` is not found.
  - **On Linux with Docker Engine** instead of Docker Desktop, start Docker with `sudo systemctl start docker`, and let your user run it without `sudo` (which runs one command as the administrator): `sudo usermod -aG docker $USER`, then log out and back in. Otherwise Docker says `permission denied` about `docker.sock`.
- **uv**, which fetches and runs JotFlow's setup, and Python 3.12 or newer, for you. See [uv's installation guide](https://docs.astral.sh/uv/getting-started/installation/). After installing it, open a new terminal so the `uv` command is found. Without uv, you need Python 3.12 or newer; the `python3` built into macOS is often older, so get Python from <https://www.python.org/downloads/>. See step 3.
- **To turn the agent on**, which you can do after the rest:
  - **an Anthropic API key**, from <https://console.anthropic.com/settings/keys>, or a Claude account to log in with instead
  - **two GitHub tokens** for the repos your channels feed, one read-only and one that can write issues. You make them on GitHub during [the agent containers](#the-agent-containers) step
- Permission to **create apps in your Slack workspace**. If **Create New App** is greyed out at <https://api.slack.com/apps>, ask a workspace admin to allow app creation. Some workspaces also need an admin to approve each new app; see [If your workspace needs admin approval](#if-your-workspace-needs-admin-approval).

## 2. Make a folder for JotFlow

JotFlow needs no copy of its source. Setup writes everything it needs into one folder: your tokens, your channels, and the two Docker Compose files that run JotFlow's published images. Make that folder and go into it.

On Windows with WSL, open a WSL terminal and type `cd ~` first, so the folder is in your Linux home directory, not under `/mnt/c`. Files on a Windows drive cannot be kept private from other Windows users, and `.env` holds your tokens.

On Windows in PowerShell, first go to a folder outside OneDrive and Documents, which sync to the cloud, for example `mkdir C:\src; cd C:\src`. Setup later warns that `.env` is on a Windows drive; that is expected there, and the warning says how to keep it private.

```
mkdir jotflow
cd jotflow
```

On a server, use `/srv/jotflow` instead, as [docs/deployment.md](deployment.md) describes.

To run JotFlow from its source instead, for example to change it, see [Run from a checkout](../CONTRIBUTING.md#run-from-a-checkout) in CONTRIBUTING.md. A checkout starts JotFlow with `make up`, and its `.env` needs no `COMPOSE_FILE` line; that section explains both.

## 3. Run the guided setup

```
uvx jotflow init
```

This works from JotFlow's first published release (#41). Before that, use [Run from a checkout](../CONTRIBUTING.md#run-from-a-checkout).

The first `uvx` downloads Python and JotFlow, so it can sit quietly for a minute before setup starts. Setup also writes `docker-compose.yml` and `docker-compose.policy.yml` into this folder, pinned to the JotFlow release you ran. It never replaces one you already have without asking, and keeps the old one as a `.bak` file if you say yes.

Without uv, create a private Python environment for JotFlow, a `.venv` folder, and install it there. On Ubuntu and Debian, use uv instead, because their `python3 -m venv` fails until the `python3-venv` package is installed. On macOS, Linux, or WSL:

```
python3 -m venv .venv
.venv/bin/pip install jotflow
.venv/bin/jotflow init
```

Without uv, on Windows in PowerShell:

```
py -3.12 -m venv .venv
.venv\Scripts\pip install jotflow
.venv\Scripts\jotflow init
```

Setup starts by saying what it will do, and explains every question just before it asks. `uvx jotflow init --help` shows the same overview. Press Enter to accept the default in brackets, or Ctrl-C to stop. Each question allows five tries; after that setup stops without changing anything, and you can run it again. `.env` and `jotflow.toml` are written only after the last question, so stopping part-way leaves them unchanged. Only the Slack manifest, `slack-app-manifest.json`, is saved earlier, so you can paste it if the link fails.

First, setup asks whether you already created a Slack app for JotFlow. Answer `n` the first time. Then it walks through six setup steps, and prints "Setup step 1 of 6" and so on to match this list:

1. **The bot's name.** This is how the bot appears in Slack, for example `Idea Bot`. It can be up to 35 characters: letters, digits, spaces, `.`, `-` and `_`, starting with a letter or digit. You can change it later in Slack.
2. **Create the Slack app.** Sign in to Slack in your browser first. Setup prints a long link. On a server over SSH, copy the link into your own computer's browser. For the `cat slack-app-manifest.json` fallback below, open a second SSH terminal in the same folder, because setup is waiting for input in the first. Open the link, pick your workspace, click **Next** if Slack shows it, then click **Create**: the manifest is already filled in. If the link is cut off, doesn't open, or Slack shows an error page, which can happen when a long link wraps across lines in the terminal, go to <https://api.slack.com/apps>, choose **Create New App**, then **From a manifest**, and paste the contents of `slack-app-manifest.json`. To see that file, type `cat slack-app-manifest.json`, or in PowerShell `Get-Content slack-app-manifest.json`, and copy all of it. Setup saved it with your bot name, so use it rather than `manifests/slack-app.json`, which is the default with the name JotFlow.
3. **The app-level token**, which lets JotFlow connect to Slack without a public URL. In the new app's settings, open **Basic Information**, scroll to **App-Level Tokens**, and click **Generate Token and Scopes**. Type any name, for example `socket-mode`; the name is only a label. Click **Add Scope**, choose `connections:write`, click **Generate**, then click **Copy**. It starts with `xapp-`. To see it again later, open **Basic Information**, scroll to **App-Level Tokens**, and click the token's name.
4. **The bot token.** Open **Install App**, click **Install to Workspace**, then **Allow**. Copy the **Bot User OAuth Token** shown on that page; it also appears under **OAuth & Permissions**, which is Slack's page for the app's permissions. It starts with `xoxb-`. If Slack shows **Request to Install** instead, see [If your workspace needs admin approval](#if-your-workspace-needs-admin-approval).

   Paste each token when asked. **Nothing appears as you paste**; that is expected, because tokens stay hidden. In some terminals you paste with a right-click. Then press Enter. Setup checks each token with Slack straight away. If a token is wrong, it says what's wrong and asks again. If a token ever leaks, [manifests/README.md](../manifests/README.md) explains how to revoke it.
5. **Channels and repos.** For each channel JotFlow should watch, such as `#ideas`, setup asks for:
   - **the repos, from a list, if you have the [GitHub CLI](https://cli.github.com/) signed in.** Setup asks whose repos to list, shows them numbered, and you type the ones to add, such as `1,3` or `2-4`. Each one's nickname, "what it covers", and public or private come from GitHub, and you can change any of them. It uses your own `gh` login to read the list, and keeps nothing but the repos you pick. Press Enter at the list to type repos instead, as below.
   - **the channel id.** Open the channel, click its name at the top, and find the **Channel ID** at the bottom of the **About** tab. It starts with `C`, or `G` for some private channels, for example `C0IDEAS01`. The channel must already exist in Slack. Pasting the channel's link works too, from **Copy link** or from your browser's address bar. The name, such as `#ideas`, does not: setup says `That is the channel's name. Setup needs its id` and asks again. Anything else that isn't an id gets `not a Slack channel id`.
   - **each GitHub repo** that channel feeds, as `owner/name`: the part after `github.com/` in its URL. For example `https://github.com/acme-co/web` is `acme-co/web`. Pasting the link works too.
   - **public or private**, unless setup could ask GitHub. With the GitHub CLI signed in, setup looks it up and says what it found. Otherwise it asks `Public or private on GitHub [skip]` straight after the repo, before the nickname. Type `public`, `private`, or press Enter to skip; cards for a public repo carry a warning, and cards for a skipped one say visibility wasn't checked.
   - **a nickname**, a short word people type to point at the repo. Later, starting a Slack message with `re: web#42` points it at issue 42 in that repo.
   - **what it covers**, in one sentence, for example `The customer-facing website`. When a channel feeds several repos, the agent uses these sentences to choose one.
   - **labels** a proposal may put on an issue. The default, `any`, allows any label that already exists on the repo. A comma-separated list, for example `bug, enhancement`, narrows it to just those; list only labels that already exist on the repo.
6. **The agent preset.** Your team supplies and controls the agent; JotFlow only needs to know which program it is so it can start it correctly. Type `claude-code` for Claude Code, or `codex` for Codex CLI. With `claude-code`, press Enter at the model question for the preset's defaults. With `codex`, setup asks for an OpenAI API key instead, hidden, or you can press Enter and add it later. Setup also sets up the agent containers' files; see [the agent containers](#the-agent-containers) below. Or press Enter at the preset for `none`, the default for now, to leave the agent off and turn it on later.

**Other agents and models.** Most teams can skip this.

- **Your own agent.** Setup also accepts `command`, if your agent files set `JOTFLOW_AGENT_COMMAND` to run your own agent. When your agent files already set a command, the default is `command`. Any other name is refused for now.
- **The model.** A model name you type at the model question applies to both drafting and execution. For a different model per run kind, set `JOTFLOW_AGENT_MODEL_DRAFTING`, `JOTFLOW_AGENT_MODEL_QUESTION`, or `JOTFLOW_AGENT_MODEL_EXECUTION` in the agent env files by hand, and setup keeps them.
- **Codex's two limits.** `codex` serves drafting and question runs only, so approved issues are still written by Claude Code, from `agent-exec.env` with its Anthropic login. Setup keeps that file on `claude-code`. Codex also refuses models other than `gpt-5.5`, as a safety check: newer models add a code-execution tool. So setup asks no model question for it. A startup check of each model's tools is planned to replace that list (#220). [docs/presets.md](presets.md) explains both limits.

A session looks like this, shortened. Your answers are on the right of each prompt; a hidden token shows nothing.

```
jotflow init sets JotFlow up for your team. It usually takes ten to fifteen
minutes, most of it in Slack's settings pages. It will:
  ...
Have you already created a Slack app for JotFlow? [y/N]:

Setup step 1 of 6: The bot's name
This is how the bot appears in Slack, for example Idea Bot. You can change it later in Slack.
Up to 35 characters: letters, digits, spaces, . - and _, starting with a letter or digit.
Bot name [JotFlow]: Idea Bot

Setup step 2 of 6: Create the Slack app
Sign in to Slack in your browser first. Then open this link, pick your workspace, click Next if Slack shows it, and click Create:

  https://api.slack.com/apps?new_app=1&manifest_json=...

Setup step 3 of 6: The app-level token
  ...
Paste the app-level token (xapp-, hidden):
  App-level token works: Socket Mode can connect.

Setup step 4 of 6: The bot token
  ...
Paste the bot token (xoxb-, hidden):
  Bot token works: @idea_bot in Acme Co.

Setup step 5 of 6: Channels and repos
  ...
Channel id: C0IDEAS01
  GitHub repo (owner/name or link): https://github.com/acme-co/web
  ...
  Public or private on GitHub [skip]: private
  ...
  Nickname [web]:
  What it covers: The customer-facing website
  Labels [any]:
  Add another repo to this channel? [y/N]:
Add another channel? [y/N]:

Setup step 6 of 6: The agent preset
  ...
Agent preset [none]: claude-code

Which model should the agent use? Press Enter for the preset's defaults: a stronger
model for drafting and questions, a faster one for writing approved issues.
  ...
Agent model [default]:

  agent.env got a new runner secret.
  agent-exec.env got a new runner secret.
Saved agent.env and agent-exec.env. Only your user can read them.
  Before the agent containers can start, fill in JOTFLOW_GITHUB_READ_TOKEN in agent.env and JOTFLOW_GITHUB_WRITE_TOKEN in agent-exec.env, and the model login, as their comments describe. Then run uvx jotflow init again, or add COMPOSE_PROFILES=agent to .env.
Saved both tokens to .env. Only your user can read that file.
  Setup also set COMPOSE_FILE there, so `docker compose up -d` hands jotflow.toml to JotFlow.
Wrote jotflow.toml: 1 channel, 1 repo, claude-code.
Wrote docker-compose.yml.
Wrote docker-compose.policy.yml.
Connected to Slack as @idea_bot in Acme Co.

Next:
  1. Invite @idea_bot to each channel JotFlow should watch with /invite @idea_bot. It never joins channels by itself.
  2. Check the setup with: uvx jotflow check
  3. Start JotFlow with: docker compose up -d
```

With `codex` as the preset, setup asks for the OpenAI API key instead of the model, and the rest is the same:

```
Agent preset [none]: codex

Codex logs in with an OpenAI API key, from https://platform.openai.com/api-keys.
  ...
Paste the OpenAI API key (sk-, hidden), or press Enter to add it later:

  agent.env got a new runner secret.
  agent-exec.env got a new runner secret.
  agent-exec.env stays on claude-code, since codex doesn't serve execution runs. It uses the Anthropic login, as its comments describe.
Saved agent.env and agent-exec.env. Only your user can read them.
```

With `none` as the preset, setup skips the model question and the agent files, and JotFlow connects to Slack but drafts nothing.

Setup writes two files next to `docker-compose.yml`. Neither is committed to git.

- **`.env`** holds the two Slack tokens. Only you can read it, on a Linux or macOS filesystem. On a Windows drive, such as `C:\` or `/mnt/c` in WSL, that is not enforced: the folder's Windows permissions decide who can read it, and setup says so instead of "Only your user can read that file". Keep your JotFlow folder somewhere private, and in WSL prefer your Linux home directory. Setup also adds a `COMPOSE_FILE` line here, so Docker picks up your `jotflow.toml`.
- **`jotflow.toml`** lists your channels and repos. It holds no secrets. Edit it freely: [jotflow.example.toml](https://github.com/Jnkman787/jotflow/blob/main/examples/jotflow.example.toml) explains every option. After editing it, run `docker compose restart jotflow` so JotFlow reads it again. `docker compose up -d` alone isn't enough, because a changed file doesn't recreate the container. Each repo can say whether it's public or private on GitHub. Setup fills that in from your own `gh` login if you have one, and asks otherwise. Cards for a public repo carry a warning.

Running `uvx jotflow init` again is safe. It offers to keep tokens that still work, and it keeps the agent containers' secrets. For an existing `jotflow.toml`, it asks `Keep its channels and repos? [Y/n]`. Answer `y` to keep them and go straight to the agent preset, which is how you turn the agent on or change it later. Answer `n` to go through the channels again, where each one's repos can be kept.

### If your workspace needs admin approval

If Slack shows **Request to Install** instead of **Install to Workspace**, an admin must approve the app before you get the bot token.

1. Click **Request to Install**, then press Ctrl-C in the terminal to stop setup. `.env` and `jotflow.toml` are not written yet, so this is safe. Only `slack-app-manifest.json` was saved, and it is fine to leave.
2. Wait for the admin to approve the app. Slack usually notifies you when they do.
3. Run `uvx jotflow init` again. When it asks `Have you already created a Slack app for JotFlow?`, answer `y`. It then skips the create link, so you don't make a second app. Open your existing app at <https://api.slack.com/apps>, under **Your Apps**, and continue from setup step 3. You can copy the app-level token again from **Basic Information**.

## 4. Invite the bot

In Slack, in each channel you listed, type:

```
/invite @idea_bot
```

Use the bot's handle, which setup printed after `Connected to Slack as`; Slack also suggests it as you type. If Slack asks whether to add the app to the channel, click **Add**. JotFlow never joins channels by itself. Joining a channel is always a person's choice.

## 5. Check the setup

```
uvx jotflow check
```

This checks that `jotflow.toml` and `.env` load exactly as JotFlow will load them, and, with an agent preset, whether the agent containers' files are filled in. It posts nothing, doesn't contact Slack, and needs no Docker. When everything is ready:

```
ok    configuration: 1 channel(s) in jotflow.toml
ok    agent: claude-code, both agent files filled in
```

Right after setup, before the agent files are filled in, each `warn` line says what's still missing:

```
ok    configuration: 1 channel(s) in jotflow.toml
warn  agent: COMPOSE_PROFILES in .env does not include agent, so docker compose up -d will not start the agent containers
warn  agent: JOTFLOW_GITHUB_READ_TOKEN is blank in agent.env
warn  agent: ANTHROPIC_API_KEY is blank in agent.env, which passes it to the agent
warn  agent: JOTFLOW_GITHUB_WRITE_TOKEN is blank in agent-exec.env
warn  agent: ANTHROPIC_API_KEY is blank in agent-exec.env, which passes it to the agent
```

Fill in the agent files as [the agent containers](#the-agent-containers) describes, then run `uvx jotflow init` again to set `COMPOSE_PROFILES`. JotFlow still starts with warnings, but drafts nothing until they're fixed.

A `fail` line means a run could never work as the files stand, and check exits with status 1. The common one is a model login the file sets but doesn't list in `JOTFLOW_AGENT_ENV_ALLOW`, so the runner never passes it to the agent. Check names the file and the whole line to set, without showing the key:

```
fail  agent: agent.env sets ANTHROPIC_API_KEY but doesn't pass it to the agent, so no run can sign in. Set this line in agent.env: JOTFLOW_AGENT_ENV_ALLOW=ANTHROPIC_API_KEY
```

Running `uvx jotflow init` again adds that line for you and says so. If check says `configuration problem` instead, fix what it names, or run `uvx jotflow init` again, before starting JotFlow. With no preset, check prints only the configuration line. Without uv, use `.venv/bin/jotflow check`, or `.venv\Scripts\jotflow check` on Windows.

Optionally, the [live check](troubleshooting.md), `check --live`, does a real round trip to Slack: it posts one test message in a channel the bot is in and deletes it. It runs while JotFlow is stopped, so do it before the next step or stop JotFlow first.

## 6. Start JotFlow

Make sure Docker is running first: Docker Desktop, or on Linux the Docker service. Otherwise Docker says `Cannot connect to the Docker daemon`, its name for the background service; start Docker, wait until it says it is running, and try again.

On Windows in PowerShell, setup has already added the `COMPOSE_PATH_SEPARATOR=:` line Docker needs to `.env`. In WSL this doesn't apply.

```
docker compose up -d
```

This starts JotFlow. The agent containers, where your team's agent will draft issues and write the approved ones, are off until you set them up.

### The agent containers

There are two agent containers, so the GitHub token that can write never sits where drafting happens. When you choose a preset, setup writes their files next to `.env`, from `agent.env.example` and `agent-exec.env.example`, readable only by you:

- **`agent.env`** is for drafting and questions. It gets the read-only GitHub token, `JOTFLOW_GITHUB_READ_TOKEN`, and the model login.
- **`agent-exec.env`** is for writing approved issues. It gets the token that can write issues, `JOTFLOW_GITHUB_WRITE_TOKEN`, and the model login. That token can create, edit, or close any issue in any repo it reaches, so limit it to exactly the repos your channels feed.

**Make the two GitHub tokens.** Make each one the same way, on GitHub:

1. Click your profile picture, then **Settings**, then **Developer settings** at the bottom of the left menu. Open **Personal access tokens**, then **Fine-grained tokens**, then click **Generate new token**.
2. Name it, for example `jotflow-read` or `jotflow-write`, and pick an expiration. Under **Resource owner**, choose the user or organisation that owns the repos. An organisation may need an owner to approve the token before it works.
3. Under **Repository access**, choose **Only select repositories**, and pick exactly the repos in `jotflow.toml`.
4. Under **Permissions**, then **Repository permissions**:
   - for the read token, set **Issues** to **Read-only**, and nothing else
   - for the write token, set **Issues** to **Read and write**, and nothing else

   GitHub adds **Metadata: Read-only** to both by itself.
5. Click **Generate token**, and copy it. It starts with `github_pat_`, and GitHub shows it only once.

When a channel later feeds a new repo, add it to both tokens on GitHub too. [docs/presets.md](presets.md) explains what each token can reach.

**For a channel with a scope,** the read token decides what JotFlow finds, and the write token decides what it may write. Give both the same repos. A found repo the write token can't see is refused at approval with a message saying so, and approved once you add it to the token. That check can't tell whether the token may write, and any token sees a public repo. If the token can't file the issue, the write fails at GitHub, and the card says so. With **All repositories** instead, the scope's patterns are the only limit on where JotFlow writes, so keep them narrow. The tokens need no extra permission: listing an owner's repos only needs **Metadata**, which GitHub adds by itself.

**The model login**, for `claude-code`, is either of these:

- **An Anthropic API key**, from <https://console.anthropic.com/settings/keys>. Put it in both files as `ANTHROPIC_API_KEY`. Separate keys let you track spend per run kind.
- **A Claude account login**, such as a Claude subscription, done once inside each container instead of a key. Remove the `ANTHROPIC_API_KEY` line, and that name from `JOTFLOW_AGENT_ENV_ALLOW`, in both files. Then run:

  ```
  docker compose run --rm --no-deps agent claude auth login
  docker compose run --rm --no-deps agent-exec claude auth login
  ```

  `--rm` removes only that one-off container. The login is saved in the service's own Docker volume, `agent-home` or `agent-exec-home`, so it survives restarts, upgrades, and `docker compose down`. Only `docker compose down -v`, which deletes volumes, removes it, and then you log in again. A login token can't go in the env file instead: the runner refuses to pass any variable ending in `_TOKEN`, such as `CLAUDE_CODE_OAUTH_TOKEN`.

**The model login, for `codex`,** is an OpenAI API key only, from <https://platform.openai.com/api-keys>. Setup asks for it and saves it in `agent.env` as `CODEX_API_KEY`. `agent-exec.env` still needs one of the Anthropic logins above, since execution runs on claude-code.

### Turn the agent on

1. Make the two GitHub tokens and get the model login, as above.
2. In `agent.env`, fill in `JOTFLOW_GITHUB_READ_TOKEN` and `ANTHROPIC_API_KEY`. In `agent-exec.env`, fill in `JOTFLOW_GITHUB_WRITE_TOKEN` and `ANTHROPIC_API_KEY`. With a Claude account login instead, remove `ANTHROPIC_API_KEY` and its `JOTFLOW_AGENT_ENV_ALLOW` entry from both files, as above, and run the two login commands after step 3. With `codex`, `agent.env` takes `CODEX_API_KEY` instead of `ANTHROPIC_API_KEY`, unless you pasted it during setup.
3. Run `uvx jotflow init` again. With working tokens in `.env`, setup skips the `Have you already created a Slack app for JotFlow?` question; if it does ask, answer `y`, so you don't make a second app. Then answer:
   - `Keep it? [Y/n]` for the app-level token, then again for the bot token: press Enter for yes
   - `Keep its channels and repos? [Y/n]`: press Enter for yes
   - `Agent preset`, which shows your choice, such as `[claude-code]` or `[codex]`: press Enter. With `claude-code`, press Enter at `Agent model` too.

   Setup keeps the runner secrets and adds `COMPOSE_PROFILES=agent` to `.env`.
4. Run `uvx jotflow check`. Every line should say `ok`, including `ok    agent: claude-code, both agent files filled in`, or the same with `codex`.
5. Run `docker compose up -d` again. JotFlow is already running from step 6, but only this starts the two agent containers.
6. Run `docker compose logs agent agent-exec`. Each runner logs what it serves, such as `runner serving drafting, question with preset claude-code` and `runner serving execution with preset claude-code`. With `codex`, the first says `preset codex`.

Setup gives each file its own runner secret and puts both in `.env`, as `JOTFLOW_RUNNER_SECRET` and `JOTFLOW_RUNNER_EXEC_SECRET`. It never shows them. It also names the preset, and the model if you chose one. Neither file holds Slack tokens, and neither agent sees `.env`.

To replace a secret, for example after it leaked, run setup with `--rotate-runner-secret` for drafting, `--rotate-exec-secret` for execution, or both. Then run `docker compose up -d` so every container picks up the new value. Setup writes `.env` and both agent files as one step: if one cannot be written, none of them changes. If `.env` and an agent file ever hold different secrets, for example after you replaced one by hand, setup makes a new secret for both rather than guessing which is right, and asks you to restart. With the `command` preset, setup writes each file's secret but no preset or model, since the runner refuses those next to a command.

Agent runs start only once `[agent] preset` is set in `jotflow.toml`, and it must name the same agent the runners use: `claude-code`, `codex`, or `command` for your own `JOTFLOW_AGENT_COMMAND`. The one exception is `codex`, which doesn't write issues: with it, the drafting runner (`agent.env`) uses `codex` and the execution runner (`agent-exec.env`) stays on `claude-code`, which JotFlow expects. At startup JotFlow asks each runner what it starts. If a runner names another preset or other kinds, or doesn't answer within about 30 seconds, JotFlow logs why and keeps that kind of run off until it restarts. Each kind of run also needs its runner's secret in `.env`. Without `JOTFLOW_RUNNER_EXEC_SECRET`, for example, JotFlow logs `execution runs are off` at startup and leaves approved proposals queued until it is set.

The first start downloads JotFlow's images, which takes a minute or two. Setup pinned them to the release you ran, `ghcr.io/jnkman787/jotflow:X.Y.Z` and `ghcr.io/jnkman787/jotflow-agent:X.Y.Z`, so nothing is built and an upgrade happens only when you change the version. [docs/deployment.md](deployment.md) covers upgrades and rollback. Each published image carries a signed build provenance attestation, which GitHub's CLI checks:

```
gh attestation verify oci://ghcr.io/jnkman787/jotflow:X.Y.Z --owner Jnkman787
```

## 7. See it working

```
docker compose logs -f jotflow
```

This keeps following the log, so open a second terminal in your JotFlow folder for any other command.

When JotFlow starts, it pins a short guide in each channel it watches. Post a message in one of them: your first message gets a one-time tip that only you can see. With an agent preset, a card with the proposed issue appears in the thread once the conversation goes quiet, which is about two minutes after the last message by default (`debounce_seconds = 120`). To see the agent containers are up and serving, run `docker compose logs agent agent-exec`. The log shows each step either way. A line like the last one below appears:

```
2026-10-18 09:41:02,118 INFO jotflow.run: watching 1 configured channel(s) as @idea_bot
2026-10-18 09:41:02,604 INFO jotflow.run: connected to Slack over Socket Mode
2026-10-18 09:41:37,250 INFO jotflow.ingest: message channel=C0IDEAS01 ts=1760781697.000100 thread_ts=None
```

In each `message` line, `ts` is the message's id in Slack, which is also the time it was posted, and `thread_ts` is the thread it is a reply in, or `None` for a new message. The log shows where a message was posted and when, never what it says, and never a token. It does show channel ids and your bot and workspace names, so remove those before pasting logs into a public issue. Adding a reaction logs a `reaction` line in the same way. Press Ctrl-C to stop following the log; JotFlow keeps running. Stop it with `docker compose down`.

To run JotFlow without Docker, use `uvx jotflow run` from the same folder instead of steps 6 and 7. It prints the same lines in that terminal, rather than to `docker compose logs`; press Ctrl-C to stop it.

## See it work without Slack

`jotflow replay` runs one of JotFlow's saved example conversations through the same steps a real channel goes through: reading messages, grouping them into topics, and drafting proposals. A scripted stand-in plays the agent. Nothing connects to Slack, GitHub, or the internet.

```
uvx jotflow replay --list
uvx jotflow replay feature-request
```

It prints what happened at each step, `ok` or `FAIL`, and whether the result matches what the example expects. The examples, and the format for saving your own, are described in [src/jotflow/testing/threads/README.md](../src/jotflow/testing/threads/README.md).

## Check that the agent can write an issue

To test the write side on its own, without drafting or approving in Slack, you can hand JotFlow one approved issue and watch the agent write it. Use a test repo, because this creates a real issue there.

You need:

- the agent containers set up, as in step 6
- `[agent] preset` set in `jotflow.toml`
- a test channel whose repo list names the test repo
- the `ts` of one message in that channel, from the log, which is where a failure notice would go

Write `fixture.json`:

```json
{
  "channel_id": "C0TEST01",
  "thread_ts": "1760781697.000100",
  "repo": "your-org/test-repo",
  "title": "JotFlow end-to-end check",
  "body": "Created by jotflow execute-fixture. Safe to delete.",
  "approved_by": "U0YOURID"
}
```

`approved_by` is your Slack member ID. In Slack, open your profile, click the **⋮** menu, and choose **Copy member ID**. It starts with `U`.

Then queue it while JotFlow runs:

```
docker compose exec -T jotflow jotflow execute-fixture - --confirm < fixture.json
```

It prints the proposal id and the run id. Within a few seconds the log shows the run start, and then end `succeeded`. The issue appears in the test repo with the proposal id hidden in its body.

**To check a retry does not duplicate the issue,** take the proposal id it printed, and do the following:

1. Add `JOTFLOW_DATABASE_PATH=/data/retry-check.db` to `.env`, and restart JotFlow with `docker compose up -d`.
2. Add `"id": "<that proposal id>"` to `fixture.json`.
3. Run the same command with `--reconciling` added. That stores the proposal as if its first run had been interrupted, so the agent looks for the hidden id before writing.
4. Check that the run ends `succeeded` and the repo still has one such issue.
5. Remove the `.env` line, restart, and delete the test issue.

The command refuses in CI, refuses without `--confirm`, and checks the repo and labels against `jotflow.toml` as a drafted proposal would be checked. It bypasses the approver list in `jotflow.toml`: `approved_by` is whatever user id the fixture names, and no one in Slack approves anything. It is an operator check that needs a shell in JotFlow's container, not a way around approvers, so keep that access to the people who run JotFlow.
