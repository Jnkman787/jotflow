"""`jotflow init`: guided setup from a fresh clone to a connected bot.

Slack requires two manual steps that no software can skip, creating the app
and clicking Allow (see the Permissions section of docs/architecture.md).
This command wraps everything around them: it renders the manifest, prints the
create-from-manifest link, checks the pasted tokens, and writes ``.env``.

Input, hidden input, output, and the Slack client are all injected, so the
whole session can be scripted in tests without a terminal or a network.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tempfile
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

from pydantic import TypeAdapter, ValidationError

from jotflow import __version__, agent_setup, deploy
from jotflow.envfile import EnvChange, read_env, write_envs
from jotflow.github_cli import ListedRepo, gh_login, gh_repos, gh_visibility
from jotflow.models import RepoName, RepoVisibility
from jotflow.policy import (
    ChannelId,
    ChannelPolicy,
    Label,
    Nickname,
    Policy,
    PolicyError,
    PresetName,
    RepoPolicy,
    load_policy,
)
from jotflow.policy_writer import render_policy
from jotflow.slack_auth import (
    BotIdentity,
    HttpSlackApi,
    SlackApi,
    SlackUnreachableError,
    TokenError,
    check_app_token,
    check_bot_token,
)
from jotflow.slack_manifest import (
    DEFAULT_BOT_NAME,
    MAX_BOT_NAME_LENGTH,
    BotNameError,
    render_manifest_json,
    validate_bot_name,
)

BOT_TOKEN_KEY = "JOTFLOW_SLACK_BOT_TOKEN"
APP_TOKEN_KEY = "JOTFLOW_SLACK_APP_TOKEN"
# Compose reads COMPOSE_FILE from .env, so plain `docker compose up -d` also
# applies docker-compose.policy.yml, which mounts jotflow.toml read-only.
COMPOSE_FILE_KEY = "COMPOSE_FILE"
COMPOSE_FILE_VALUE = "docker-compose.yml:docker-compose.policy.yml"
# Compose splits COMPOSE_FILE on ";" on Windows unless told otherwise, so setup
# sets the separator itself there rather than asking people to edit .env.
COMPOSE_SEPARATOR_KEY = "COMPOSE_PATH_SEPARATOR"
COMPOSE_SEPARATOR_VALUE = ":"
# jotflow.toml holds no secrets, and the container's uid must be able to read it.
POLICY_FILE_MODE = 0o644

CREATE_APP_URL = "https://api.slack.com/apps"
DEFAULT_MANIFEST_OUT = "slack-app-manifest.json"
MAX_ATTEMPTS = 5
# Agent runs arrive in milestone 3, so a fresh setup connects without one.
DEFAULT_PRESET: str | None = None
FIRST_PRESET = "claude-code"
# The team's own JOTFLOW_AGENT_COMMAND, as the runner reports it in GET /info (#142).
COMMAND_PRESET = "command"
# Presets setup accepts today; others are refused until JotFlow ships them.
SUPPORTED_PRESETS = (FIRST_PRESET, COMMAND_PRESET)
STEPS = 6
ANY_LABEL = "any"
MAX_CHANNELS = 50
MAX_REPOS_PER_CHANNEL = 20

# Everything init says to explain itself. Prompts, --help, and tests share it.
# Examples are fictional: acme-co, #ideas, Idea Bot. Never a real org or workspace.
INTRO = f"""\
jotflow init sets JotFlow up for your team. It usually takes ten to fifteen
minutes, most of it in Slack's settings pages. It will:

  1. Help you create a Slack app for JotFlow from a link with everything filled in.
  2. Check the two tokens Slack gives you for that app.
  3. Ask which Slack channels JotFlow should watch, such as #ideas, and which
     GitHub repos each one feeds, such as acme-co/web.
  4. Write .env, with the Slack tokens and readable only by you on Linux and
     macOS, and jotflow.toml, with your channels and repos and no secrets.
     If you choose an agent preset, also write agent.env and agent-exec.env
     for the agent containers, each with its own new secret.

.env and jotflow.toml are written only after the last question; only the
Slack manifest, slack-app-manifest.json, is saved earlier so you can paste
it if the link fails. Press Ctrl-C at any prompt to stop. Each question
allows {MAX_ATTEMPTS} tries; after that setup stops without changing anything,
and you can run it again."""

EXPLAIN_BOT_NAME = (
    "This is how the bot appears in Slack, for example Idea Bot. You can change it later in Slack.",
    f"Up to {MAX_BOT_NAME_LENGTH} characters: letters, digits, spaces, . - and _, "
    "starting with a letter or digit.",
)
EXPLAIN_EXISTING_APP = (
    "If you already created JotFlow's Slack app, for example on an earlier run while "
    "waiting for an admin to approve it, answer y to reuse it instead of creating another."
)
EXPLAIN_REUSE_APP = (
    f"Open your existing app at {CREATE_APP_URL}, under Your Apps, and continue from its "
    "settings. If its install is still waiting for an admin, press Ctrl-C now and run setup "
    "again once it is approved."
)
EXPLAIN_CREATE_LINK = (
    "Sign in to Slack in your browser first. Then open this link, pick your workspace, "
    "click Next if Slack shows it, and click Create:"
)
EXPLAIN_PASTE = (
    "Nothing appears as you paste; that is expected, the token stays hidden. "
    "In some terminals, right-click to paste. Then press Enter."
)
EXPLAIN_APP_TOKEN = (
    "The app-level token lets JotFlow connect to Slack without a public URL.",
    "In the new app's settings, open Basic Information, scroll to App-Level Tokens, "
    "and click Generate Token and Scopes.",
    "Name it anything, for example socket-mode; the name is only a label. "
    "Click Add Scope, choose connections:write, then click Generate.",
    "Click Copy next to the token. It starts with xapp-.",
)
EXPLAIN_BOT_TOKEN = (
    "Now install the app: open Install App, click Install to Workspace, then click Allow.",
    "If Slack shows Request to Install instead, your workspace needs an admin to approve "
    "the app. Press Ctrl-C, wait for the approval, then run setup again and answer y when "
    "it asks whether you already created the app.",
    "Copy the Bot User OAuth Token shown on the Install App page; it also appears under "
    "OAuth & Permissions. It starts with xoxb-.",
)
EXPLAIN_CHANNEL = (
    "Which Slack channel should JotFlow watch? Open the channel, click its name at the "
    "top, and find the Channel ID at the bottom of the About tab.",
    "It starts with C, or G for some private channels, for example C0IDEAS01. "
    "Pasting the channel's link works too.",
)
EXPLAIN_NEXT_CHANNEL = "The next channel's id, for example C0IDEAS02, or its link."
EXPLAIN_REPO = (
    "Which GitHub repo does this channel feed? Give its address as owner/name, the part "
    "after github.com/ in its URL.",
    "For example https://github.com/acme-co/web is acme-co/web. Pasting the link works too.",
)
EXPLAIN_VISIBILITY = (
    "Is that repo public or private on GitHub? JotFlow can't look, so it asks. A public "
    "repo puts a warning on every card, because approving makes the issue visible to "
    "anyone. Type skip if you're not sure; cards then say visibility was not checked."
)
EXPLAIN_NICKNAME = (
    "A short word people type to point at this repo: re: web#42 in Slack later points a "
    "message at issue 42 here. Lowercase is best. Press Enter to keep the suggestion "
    "in brackets."
)
EXPLAIN_DESCRIPTION = (
    "One sentence on what this repo is for, for example The customer-facing website.",
    "When a channel feeds several repos, the agent uses these sentences to choose one.",
)
EXPLAIN_LABELS = (
    "Labels a proposal may put on an issue. List only labels that already exist on the "
    "repo, comma-separated, for example bug, enhancement.",
    "Press Enter for any: any label that already exists on the repo. A list narrows it.",
)
EXPLAIN_PRESET = (
    "Which agent program will your team run? Your team supplies and controls the agent;",
    "JotFlow only needs to know which program it is so it can start it correctly.",
    f"Choosing {FIRST_PRESET} also sets up agent.env and agent-exec.env for the two agent",
    "containers, each with its own new secret. Type none to leave the agent off for now.",
    f"Type {COMMAND_PRESET} if your agent files set JOTFLOW_AGENT_COMMAND to run your own agent.",
)
EXPLAIN_MODEL = (
    "Which model should the agent use? Press Enter for the preset's defaults: a stronger",
    "model for drafting and questions, a faster one for writing approved issues.",
    "A name you type is used for both drafting and execution, in agent.env and agent-exec.env.",
    "Type default to go back to the preset's defaults.",
    "For a different model per kind, set JOTFLOW_AGENT_MODEL_DRAFTING, _QUESTION, or",
    "_EXECUTION in agent.env or agent-exec.env by hand; setup keeps them.",
)
AGENT_FILE = "agent.env"
AGENT_EXEC_FILE = "agent-exec.env"
# Matches the runner's own check on model names (#137).
_MODEL = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
STEP_TITLES = {
    1: "The bot's name",
    2: "Create the Slack app",
    3: "The app-level token",
    4: "The bot token",
    5: "Channels and repos",
    6: "The agent preset",
}
_WINDOWS_DRIVE = re.compile(r"/mnt/[A-Za-z](/|$)")


class SetupAborted(Exception):
    """The person stopped the session, or input ran out."""


@dataclass
class Console:
    """Prompts and messages. Each function can be replaced in tests."""

    read: Callable[[str], str] = input
    read_secret: Callable[[str], str] = getpass.getpass
    out: TextIO = field(default_factory=lambda: sys.stdout)

    def say(self, message: str = "") -> None:
        print(message, file=self.out)

    def _read(self, reader: Callable[[str], str], prompt: str) -> str:
        try:
            return reader(prompt)
        except (EOFError, KeyboardInterrupt):
            raise SetupAborted from None

    def ask(
        self,
        question: str,
        default: str | None = None,
        validate: Callable[[str], str] | None = None,
    ) -> str:
        """Ask until the answer is valid. ``validate`` returns the cleaned value or raises."""
        suffix = f" [{default}]" if default else ""
        for _ in range(MAX_ATTEMPTS):
            answer = self._read(self.read, f"{question}{suffix}: ").strip()
            if not answer and default is not None:
                answer = default
            if validate is None:
                if answer:
                    return answer
                self.say("  Please enter a value.")
                continue
            try:
                return validate(answer)
            except ValueError as exc:
                self.say(f"  {exc}")
        raise SetupAborted

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        for _ in range(MAX_ATTEMPTS):
            answer = self._read(self.read, f"{question} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.say("  Please answer y or n.")
        raise SetupAborted

    def secret(self, question: str) -> str:
        return self._read(self.read_secret, f"{question}: ").strip()


def _step(console: Console, number: int) -> None:
    """A heading that matches the README's numbered setup steps."""
    console.say(f"Setup step {number} of {STEPS}: {STEP_TITLES[number]}")


def on_windows_drive(path: Path) -> bool:
    """Whether ``path`` is on a Windows filesystem, where file modes are not enforced."""
    return os.name == "nt" or bool(_WINDOWS_DRIVE.match(str(path.resolve())))


def _saved_tokens_message(path: Path) -> str:
    if on_windows_drive(path):
        return (
            f"Saved both tokens to {path}. It is on a Windows drive, so the folder's Windows "
            "permissions, not JotFlow, decide who can read it. Keep the folder private; "
            "in WSL, prefer a checkout in your Linux home directory."
        )
    return f"Saved both tokens to {path}. Only your user can read that file."


def _explain(console: Console, lines: str | tuple[str, ...], indent: str = "") -> None:
    """Print the explanation that goes before a prompt."""
    for line in (lines,) if isinstance(lines, str) else lines:
        console.say(f"{indent}{line}")


def _default_config_file() -> Path:
    return Path(os.environ.get("JOTFLOW_CONFIG") or "jotflow.toml")


@dataclass(frozen=True)
class InitOptions:
    env_file: Path = Path(".env")
    manifest_out: Path = Path(DEFAULT_MANIFEST_OUT)
    config_file: Path = field(default_factory=_default_config_file)
    windows: bool = os.name == "nt"
    rotate_runner_secret: bool = False
    rotate_exec_secret: bool = False
    # The release whose images the Compose files pin outside a checkout (#87).
    version: str = __version__

    @property
    def agent_file(self) -> Path:
        return self.env_file.with_name(AGENT_FILE)

    @property
    def agent_exec_file(self) -> Path:
        return self.env_file.with_name(AGENT_EXEC_FILE)


def create_app_link(manifest_json: str) -> str:
    """The link that opens Slack's create-app dialog with the manifest filled in."""
    query = urllib.parse.urlencode({"new_app": "1", "manifest_json": manifest_json})
    return f"{CREATE_APP_URL}?{query}"


def _bot_name(value: str) -> str:
    try:
        return validate_bot_name(value)
    except BotNameError as exc:
        raise ValueError(str(exc)) from None


def _existing_bot(console: Console, api: SlackApi, token: str) -> BotIdentity | None:
    """Check a bot token already in ``.env``. Return it if it still works."""
    try:
        identity = check_bot_token(api, token)
    except TokenError as exc:
        console.say(f"The bot token in your .env no longer works: {exc}")
        return None
    except SlackUnreachableError as exc:
        console.say(f"Could not check the bot token in your .env: {exc}")
        return None
    console.say(f"Your .env already has a working bot token for {_describe(identity)}.")
    return identity if console.confirm("Keep it?") else None


def _existing_app_token(console: Console, api: SlackApi, token: str) -> bool:
    try:
        check_app_token(api, token)
    except TokenError as exc:
        console.say(f"The app-level token in your .env no longer works: {exc}")
        return False
    except SlackUnreachableError as exc:
        console.say(f"Could not check the app-level token in your .env: {exc}")
        return False
    console.say("Your .env already has a working app-level token.")
    return console.confirm("Keep it?")


def _describe(identity: BotIdentity) -> str:
    team = identity.team or identity.team_id or "your workspace"
    bot = f"@{identity.bot_user}" if identity.bot_user else "the bot"
    return f"{bot} in {team}"


def _paste_until_valid[T](
    console: Console, question: str, check: Callable[[str], T]
) -> tuple[str, T]:
    _explain(console, EXPLAIN_PASTE)
    for _ in range(MAX_ATTEMPTS):
        token = console.secret(question)
        try:
            return token, check(token)
        except TokenError as exc:
            console.say(f"  {exc}")
        except SlackUnreachableError as exc:
            console.say(f"  {exc}")
            if not console.confirm("Try again?"):
                raise SetupAborted from None
    console.say("  Too many attempts.")
    raise SetupAborted


def _create_app_steps(console: Console, options: InitOptions) -> None:
    _explain(console, EXPLAIN_EXISTING_APP)
    if console.confirm("Have you already created a Slack app for JotFlow?", default=False):
        _explain(console, EXPLAIN_REUSE_APP)
        console.say()
        return
    console.say()
    _step(console, 1)
    _explain(console, EXPLAIN_BOT_NAME)
    name = console.ask("Bot name", DEFAULT_BOT_NAME, _bot_name)
    manifest = render_manifest_json(name)
    options.manifest_out.write_text(manifest, encoding="utf-8")
    console.say()
    _step(console, 2)
    _explain(console, EXPLAIN_CREATE_LINK)
    console.say()
    console.say(f"  {create_app_link(manifest)}")
    console.say()
    console.say(
        f"If the link is cut off or does not open, choose Create New App, then From a "
        f"manifest, at {CREATE_APP_URL} and paste the contents of {options.manifest_out}, "
        "which setup just saved with your bot name."
    )
    console.say()


# --- jotflow.toml ---------------------------------------------------------


def _checker(type_: Any) -> Callable[[str], str]:
    """Turn a policy field type into a ``Console.ask`` validator with its own message."""
    adapter: TypeAdapter[str] = TypeAdapter(type_)

    def check(value: str) -> str:
        try:
            return adapter.validate_python(value)
        except ValidationError as exc:
            message = str(exc.errors(include_url=False)[0]["msg"])
            raise ValueError(message.removeprefix("Value error, ")) from None

    return check


_check_channel = _checker(ChannelId)
_check_repo = _checker(RepoName)
_check_nickname = _checker(Nickname)
_check_preset = _checker(PresetName)
_check_label = _checker(Label)

# A channel's link, as Copy link gives it (.../archives/C...) or as the browser
# shows it (app.slack.com/client/T.../C...).
_CHANNEL_LINK = re.compile(
    r"https://[^\s/]+\.slack\.com/(?:archives|client/[A-Za-z0-9]+)/([A-Za-z0-9]+)(?:[/?#].*)?"
)
_GITHUB_LINK = re.compile(r"(?:https?://)?github\.com/([^/\s]+/[^/\s]+?)(?:\.git)?/?")


def _channel_id(value: str) -> str:
    """Accept a channel id or a pasted channel link."""
    if value.strip().startswith("#"):
        raise ValueError(
            "That is the channel's name. Setup needs its id, which starts with C or G: "
            "click the channel's name at the top and look at the bottom of the About tab."
        )
    if match := _CHANNEL_LINK.fullmatch(value.strip()):
        value = match.group(1)
    value = value.strip()
    try:
        return _check_channel(value)
    except ValueError as exc:
        problem = exc
    # Never guess: a name such as customer12 would pass once uppercased, so a value
    # that only fits the id pattern in capitals is refused with a hint.
    shouted = value.upper()
    if shouted != value and _fits_id(shouted):
        if any(ch.isdigit() for ch in value):
            raise ValueError(
                f"Slack channel ids use capital letters, like C0123456789. If {value} is "
                f"the id, did you mean {shouted}? If it is the channel's name, copy the id "
                "from the channel's details instead, or paste the channel link."
            ) from None
        raise ValueError(
            "That looks like the channel's name. Setup needs its id, which starts with C or G: "
            "click the channel's name at the top and look at the bottom of the About tab, "
            "or paste the channel link."
        ) from None
    raise ValueError(f"{problem}. Copy the id exactly as Slack shows it, in capitals.") from None


def _fits_id(value: str) -> bool:
    try:
        _check_channel(value)
    except ValueError:
        return False
    return True


def _repo(value: str) -> str:
    """Accept ``owner/name`` or a pasted GitHub link."""
    value = value.strip()
    if match := _GITHUB_LINK.fullmatch(value):
        value = match.group(1)
    return _check_repo(value.removesuffix(".git"))


def _suggest_nickname(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1].lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-_")
    return name[:32] or "repo"


def _visibility(value: str) -> str:
    answer = value.strip().lower()
    if answer not in ("public", "private", "skip"):
        raise ValueError("Answer public, private, or skip.")
    return answer


def _description(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Please describe what the repo covers, in a sentence.")
    if len(value) > 200:
        raise ValueError(f"Keep the description to 200 characters; this one has {len(value)}.")
    return value


def _labels(value: str) -> str:
    """Validate the labels answer: ``any``, or a comma-separated list that narrows it."""
    if value.strip().lower() in {"", ANY_LABEL}:
        return ANY_LABEL
    if value.strip().lower() == "none":
        raise ValueError(
            "Type any to allow every label the repo already has, or list the labels to allow."
        )
    labels = [part.strip() for part in value.split(",") if part.strip()]
    seen: set[str] = set()
    for label in labels:
        _check_label(label)
        if label.casefold() in seen:
            raise ValueError(f"{label!r} is listed twice.")
        seen.add(label.casefold())
    return ", ".join(labels) if labels else ANY_LABEL


def _split_labels(answer: str) -> tuple[str, ...]:
    if answer == ANY_LABEL:
        return ()
    return tuple(part.strip() for part in answer.split(","))


def _summary(policy: Policy) -> str:
    repos = sum(len(c.repos) for c in policy.channels.values())
    channels = len(policy.channels)
    preset = policy.agent.preset or "no agent preset"
    return (
        f"{channels} channel{'s' if channels != 1 else ''}, "
        f"{repos} repo{'s' if repos != 1 else ''}, {preset}"
    )


def _ask_repo(
    console: Console,
    taken_repos: set[str],
    taken_nicknames: set[str],
    listed: ListedRepo | None = None,
) -> RepoPolicy:
    def new_repo(value: str) -> str:
        repo = _repo(value)
        if repo.casefold() in taken_repos:
            raise ValueError(f"{repo} is already listed for this channel.")
        return repo

    def new_nickname(value: str) -> str:
        nickname = _check_nickname(value.strip().lower())
        if nickname in taken_nicknames:
            raise ValueError(f"The nickname {nickname!r} is already used in this channel.")
        return nickname

    if listed is not None:
        try:
            # The same check as a typed repo, so a strange listing fails closed.
            repo = new_repo(listed.repo)
        except ValueError:
            # Don't echo the listing: it's GitHub's text, not the person's.
            console.say("  One picked repo isn't a usable owner/name, so type it instead.")
            listed = None
    if listed is not None:
        console.say()
        console.say(f"  {repo}")
        visibility = listed.visibility
    else:
        _explain(console, EXPLAIN_REPO, "  ")
        repo = console.ask("  GitHub repo (owner/name or link)", validate=new_repo)
        visibility = gh_visibility(repo)
    if listed is not None and visibility is not None:
        pass  # the listing already said, so there is nothing to ask
    elif visibility is not None:
        console.say(f"  GitHub says {repo} is {visibility}.")
    else:
        _explain(console, EXPLAIN_VISIBILITY, "  ")
        answer = console.ask("  Public or private on GitHub", "skip", _visibility)
        visibility = None if answer == "skip" else cast(RepoVisibility, answer)
    _explain(console, EXPLAIN_NICKNAME, "  ")
    nickname = console.ask("  Nickname", _suggest_nickname(repo), new_nickname)
    _explain(console, EXPLAIN_DESCRIPTION, "  ")
    suggested = listed.description if listed is not None else ""
    try:
        default = _description(suggested) if suggested else None
    except ValueError:
        default = None  # too long for a default: the person writes their own
    description = console.ask("  What it covers", default, validate=_description)
    _explain(console, EXPLAIN_LABELS, "  ")
    labels = console.ask("  Labels", ANY_LABEL, _labels)
    # "any" is None: no labels line in jotflow.toml, which means any existing
    # label. An explicit empty list would be an error there.
    chosen = _split_labels(labels) or None
    return RepoPolicy(
        repo=repo,
        nickname=nickname,
        description=description,
        labels=chosen,
        visibility=visibility,
    )


EXPLAIN_PICK = (
    "Your GitHub CLI is signed in, so setup can list repos to pick from. It only reads,",
    "with your own login, and keeps nothing but the repos you pick.",
)
_SELECTION = re.compile(r"\s*\d+(\s*-\s*\d+)?\s*(,\s*\d+(\s*-\s*\d+)?\s*)*")


def _selection(count: int) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not value.strip():
            return ""
        if not _SELECTION.fullmatch(value):
            raise ValueError("Type numbers from the list, such as 1,3 or 2-4, or press Enter.")
        for part in value.split(","):
            low, _, high = part.partition("-")
            first, last = int(low), int(high or low)
            if not 1 <= first <= last <= count:
                raise ValueError(f"Pick numbers from 1 to {count}.")
        if len(_picked(value)) > MAX_REPOS_PER_CHANNEL:
            raise ValueError(f"A channel can feed at most {MAX_REPOS_PER_CHANNEL} repos.")
        return value

    return check


def _picked(value: str) -> list[int]:
    chosen: list[int] = []
    for part in value.split(","):
        low, _, high = part.partition("-")
        for number in range(int(low), int(high or low) + 1):
            if number not in chosen:
                chosen.append(number)
    return chosen


def _pick_repos(console: Console) -> list[ListedRepo]:
    """Repos picked from the installer's own ``gh`` listing (#91), or none to type them."""
    login = gh_login()
    if login is None:
        return []
    _explain(console, EXPLAIN_PICK, "  ")
    owner = console.ask("  GitHub organisation or user to list", login)
    listed = gh_repos(owner)
    if not listed:
        console.say(f"  GitHub didn't list any repos for {owner}, so type them instead.")
        return []
    if len(listed) >= 200:
        console.say("  Showing the first 200. Type any other repo after picking.")
    width = len(str(len(listed)))
    for number, item in enumerate(listed, 1):
        about = item.description[:60] + ("…" if len(item.description) > 60 else "")
        seen = f"  ({item.visibility})" if item.visibility else ""
        console.say(f"  {number:>{width}}  {item.repo}{seen}  {about}".rstrip())
    answer = console.ask(
        "  Repos to add (such as 1,3 or 2-4; Enter to type one instead)",
        "",
        _selection(len(listed)),
    )
    return [listed[n - 1] for n in _picked(answer)] if answer else []


def _ask_channel_repos(console: Console) -> tuple[RepoPolicy, ...]:
    repos: list[RepoPolicy] = []
    for listed in _pick_repos(console):
        repos.append(
            _ask_repo(
                console, {r.repo.casefold() for r in repos}, {r.nickname for r in repos}, listed
            )
        )
    if repos and (
        len(repos) >= MAX_REPOS_PER_CHANNEL
        or not console.confirm("  Type another repo for this channel?", default=False)
    ):
        return tuple(repos)
    while True:
        repos.append(
            _ask_repo(
                console,
                {r.repo.casefold() for r in repos},
                {r.nickname for r in repos},
            )
        )
        if len(repos) >= MAX_REPOS_PER_CHANNEL or not console.confirm(
            "  Add another repo to this channel?", default=False
        ):
            return tuple(repos)


def _preset_answer(value: str) -> str:
    """``none``, or a preset JotFlow ships. Anything else is refused for now."""
    answer = value.strip().lower()
    if answer == "none":
        return answer
    preset = _check_preset(answer)
    if preset not in SUPPORTED_PRESETS:
        raise ValueError(
            f"JotFlow does not ship a {preset!r} preset yet. Type none for now, or one of: "
            + ", ".join(SUPPORTED_PRESETS)
            + "."
        )
    return preset


def _ask_policy(console: Console, existing: Policy | None, detected: str | None = None) -> Policy:
    console.say()
    _step(console, 5)
    console.say("Now the channels JotFlow watches and the repos each one feeds.")
    if existing is not None:
        listed = ", ".join(existing.channels)
        console.say(
            f"Currently listed: {listed}. List each channel to keep; "
            "a channel you leave out is removed."
        )
    channels: dict[str, ChannelPolicy] = {}
    while True:

        def new_channel(value: str) -> str:
            channel_id = _channel_id(value)
            if channel_id in channels:
                raise ValueError(f"{channel_id} is already listed.")
            return channel_id

        console.say()
        _explain(console, EXPLAIN_NEXT_CHANNEL if channels else EXPLAIN_CHANNEL)
        channel_id = console.ask("Channel id", validate=new_channel)
        previous = existing.channels.get(channel_id) if existing else None
        if previous is not None:
            nicknames = ", ".join(r.nickname for r in previous.repos)
            if console.confirm(f"  Keep this channel's current repos ({nicknames})?"):
                channels[channel_id] = previous
            else:
                channels[channel_id] = ChannelPolicy(
                    repos=_ask_channel_repos(console),
                    debounce_seconds=previous.debounce_seconds,
                )
        else:
            channels[channel_id] = ChannelPolicy(repos=_ask_channel_repos(console))
        if len(channels) >= MAX_CHANNELS or not console.confirm(
            "Add another channel?", default=False
        ):
            break

    preset = _ask_preset(console, existing, detected)

    data = existing.model_dump() if existing else {}
    # Keep any other agent settings, such as max_concurrent_runs; change only the preset.
    data["agent"] = {**(existing.agent.model_dump() if existing else {}), "preset": preset}
    data["channels"] = {cid: channel.model_dump() for cid, channel in channels.items()}
    return Policy.model_validate(data)


def _ask_preset(console: Console, existing: Policy | None, detected: str | None) -> str | None:
    """Setup step 6: which agent the team runs. ``None`` means no agent runs."""
    console.say()
    _step(console, 6)
    _explain(console, EXPLAIN_PRESET)
    current = existing.agent.preset if existing else DEFAULT_PRESET
    if current is None and detected is not None:
        current = detected
    if current is not None and current not in SUPPORTED_PRESETS:
        console.say(
            f"Your jotflow.toml names the preset {current}, which JotFlow does not ship yet, "
            "so the default below is none. Press Enter to switch to none."
        )
        current = None
    preset_answer = console.ask(
        "Agent preset",
        current or "none",
        _preset_answer,
    )
    return None if preset_answer == "none" else preset_answer


def _policy_step(console: Console, options: InitOptions) -> Policy | None:
    """Ask for the policy. ``None`` means keep the file that is there."""
    path = options.config_file
    existing: Policy | None = None
    if path.exists():
        try:
            existing = load_policy(path)
        except PolicyError as exc:
            console.say()
            console.say(f"{path} has problems:")
            for problem in exc.problems:
                console.say(f"  {problem}")
            if not console.confirm(f"Write a new {path}? The old one is kept as {path}.bak"):
                console.say(f"  Keeping {path}. jotflow run will not start until it is fixed.")
                return None
        else:
            console.say()
            console.say(f"{path} is already set up: {_summary(existing)}.")
            console.say(
                "  Keep it to change only the agent preset next. Answer n to go through the "
                "channels again; each one's repos can be kept."
            )
            if console.confirm("Keep its channels and repos?"):
                _warn_if_unreadable(console, path)
                # The preset is still asked, so a team can turn the agent on later (#41).
                preset = _ask_preset(console, existing, _detected_preset(options))
                if preset == existing.agent.preset:
                    return None
                agent = existing.agent.model_copy(update={"preset": preset})
                return existing.model_copy(update={"agent": agent})
    return _ask_policy(console, existing, _detected_preset(options))


def _detected_preset(options: InitOptions) -> str | None:
    """``command`` when an agent env file already runs the team's own command."""
    files = (read_env(options.agent_file), read_env(options.agent_exec_file))
    return COMMAND_PRESET if any(agent_setup.uses_command(f) for f in files) else None


def _warn_if_unreadable(console: Console, path: Path) -> None:
    """The container reads jotflow.toml as another user, so it must be world-readable."""
    if os.name == "posix" and not path.stat().st_mode & 0o004:
        console.say(
            f"  {path} is not readable by other users, so the JotFlow container cannot "
            f"read it. Fix it with: chmod 644 {path}"
        )


def _write_text_atomic(path: Path, text: str, mode: int = POLICY_FILE_MODE) -> None:
    directory = path.parent if str(path.parent) else Path(".")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    os.chmod(path, mode)


def _save_policy(console: Console, path: Path, policy: Policy) -> None:
    text = render_policy(policy)
    if path.exists():
        old = path.read_bytes()
        if old == text.encode("utf-8"):
            console.say(f"{path} is unchanged.")
            return
        backup = path.with_name(path.name + ".bak")
        _write_text_atomic(backup, old.decode("utf-8", errors="replace"))
        console.say(f"Saved the previous {path} as {backup}.")
    _write_text_atomic(path, text)
    console.say(f"Wrote {path}: {_summary(policy)}.")


def _chosen_preset(options: InitOptions, policy: Policy | None) -> str | None:
    """The preset from the policy being written, or from the jotflow.toml being kept."""
    if policy is not None:
        return policy.agent.preset
    try:
        return load_policy(options.config_file).agent.preset
    except (PolicyError, OSError):
        return None


def _model_answer(value: str) -> str:
    answer = value.strip()
    if answer.lower() == "default":
        return "default"
    if answer and not _MODEL.fullmatch(answer):
        raise ValueError(
            "A model name is letters, digits, and . _ : [ ] -, up to 100 characters, "
            "for example sonnet."
        )
    return answer


@dataclass(frozen=True)
class AgentChoice:
    """What setup will write to the agent env files. ``model`` None removes it."""

    preset: str
    change_model: bool = False
    model: str | None = None


def _ask_agent(console: Console, options: InitOptions, preset: str | None) -> AgentChoice | None:
    if preset is None:
        if options.rotate_runner_secret or options.rotate_exec_secret:
            console.say("  No agent preset is chosen, so there is no runner secret to rotate.")
        return None
    if preset == COMMAND_PRESET:
        # The runner refuses model settings next to a raw command.
        return AgentChoice(preset)
    files = [read_env(options.agent_file), read_env(options.agent_exec_file)]
    current = next((f[agent_setup.MODEL_KEY] for f in files if f.get(agent_setup.MODEL_KEY)), None)
    console.say()
    _explain(console, EXPLAIN_MODEL)
    answer = console.ask("Agent model", current, _model_answer)
    if answer == "default":
        return AgentChoice(preset, change_model=current is not None)
    if not answer or answer == current:
        return AgentChoice(preset)
    return AgentChoice(preset, change_model=True, model=answer)


_HOW = {
    "kept": "kept its runner secret",
    "copied": "filled in its runner secret, so both files hold the same one",
    "regenerated": (
        "held a different runner secret from .env, so setup made a new one for both. "
        "Neither old value is kept"
    ),
    "generated": "got a new runner secret",
    "rotated": "got a new runner secret, as asked",
}


def _example(path: Path) -> str:
    """The shipped example's text for a new agent env file, so its comments come along.

    From beside the file in a checkout, or from the package outside one (#87).
    """
    name = path.name + ".example"
    try:
        return path.with_name(name).read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        return deploy.template(name)
    except (ValueError, OSError):
        return ""


def in_checkout(folder: Path) -> bool:
    """Whether ``folder`` is a JotFlow source checkout, whose Compose files build from source."""
    if (folder / "Dockerfile").is_file():
        return True
    try:
        project = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    table = project.get("project")
    return isinstance(table, dict) and table.get("name") == "jotflow"


def _plan_compose(console: Console, options: InitOptions) -> list[tuple[Path, str]]:
    """The Compose files to write outside a checkout (#87): pinned to this release, and
    an existing one replaced only if the person agrees. Asked before anything is written."""
    folder = options.env_file.parent if str(options.env_file.parent) else Path(".")
    if in_checkout(folder):
        return []
    try:
        version = deploy.release_version(options.version)
    except deploy.NoPublishedImageError as exc:
        console.say(f"  {exc}. Setup writes no Compose file; build from a checkout instead.")
        return []
    wanted = [
        (
            folder / deploy.COMPOSE,
            deploy.published_compose(deploy.template(deploy.COMPOSE), version),
        ),
        (folder / deploy.COMPOSE_POLICY, deploy.template(deploy.COMPOSE_POLICY)),
    ]
    planned: list[tuple[Path, str]] = []
    for path, text in wanted:
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            planned.append((path, text))
            continue
        if current == text:
            continue
        console.say()
        if console.confirm(
            f"{path} is already here and differs from JotFlow {version}'s. Replace it? "
            f"The old one is kept as {path.name}.bak",
            default=False,
        ):
            planned.append((path, text))
        else:
            console.say(f"  Keeping your {path}.")
    return planned


def _write_compose(console: Console, planned: list[tuple[Path, str]]) -> None:
    for path, text in planned:
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            _write_text_atomic(backup, path.read_text(encoding="utf-8"))
        _write_text_atomic(path, text)
        console.say(f"Wrote {path}.")


@dataclass
class AgentFiles:
    """The agent env files' changes, what ``.env`` needs, and what to say once written."""

    changes: list[EnvChange]
    env_updates: dict[str, str]
    notes: list[str]


def _plan_agent_files(options: InitOptions, choice: AgentChoice, env: dict[str, str]) -> AgentFiles:
    agent, agent_exec = read_env(options.agent_file), read_env(options.agent_exec_file)
    drafting, execution = agent_setup.choose_secrets(
        env,
        agent,
        agent_exec,
        rotate_drafting=options.rotate_runner_secret,
        rotate_execution=options.rotate_exec_secret,
    )
    pairs = [
        (options.agent_file, agent, drafting),
        (options.agent_exec_file, agent_exec, execution),
    ]
    changes: list[EnvChange] = []
    notes: list[str] = []
    for path, values, secret in pairs:
        updates: dict[str, str | None] = {agent_setup.AGENT_SECRET_KEY: secret.value}
        command = agent_setup.uses_command(values)
        if choice.preset == COMMAND_PRESET:
            # The runner refuses a preset next to a command, so a stray one goes.
            updates[agent_setup.PRESET_KEY] = None
            if not command:
                notes.append(
                    f"  {path} has no {agent_setup.COMMAND_KEY}. Set it to your agent's command, "
                    "since jotflow.toml names the command preset."
                )
        elif command:
            notes.append(
                f"  {path} sets {agent_setup.COMMAND_KEY}, so setup left the preset and model out "
                f"of it, and JotFlow will refuse that runner while jotflow.toml names "
                f"{choice.preset}. Remove that line, or run setup again and choose "
                f"{COMMAND_PRESET}."
            )
        else:
            updates[agent_setup.PRESET_KEY] = choice.preset
            if choice.change_model:
                updates[agent_setup.MODEL_KEY] = choice.model
        changes.append((path, updates, _example(path)))
        notes.append(f"  {path} {_HOW[secret.how]}.")
    notes.append(_saved_agent_message(options))

    env_updates = {
        agent_setup.ENV_DRAFTING_KEY: drafting.value,
        agent_setup.ENV_EXECUTION_KEY: execution.value,
    }
    if agent.get(agent_setup.READ_TOKEN_KEY) and agent_exec.get(agent_setup.WRITE_TOKEN_KEY):
        profiles = agent_setup.with_agent_profile(env.get(agent_setup.PROFILES_KEY))
        if profiles is not None:
            env_updates[agent_setup.PROFILES_KEY] = profiles
            notes.append(
                f"  Also set {agent_setup.PROFILES_KEY}={profiles} in {options.env_file}, so "
                "`docker compose up -d` starts the agent containers too."
            )
    else:
        notes.append(
            f"  Before the agent containers can start, fill in {agent_setup.READ_TOKEN_KEY} in "
            f"{options.agent_file} and {agent_setup.WRITE_TOKEN_KEY} in {options.agent_exec_file}, "
            "and the model login, as their comments describe. Then run jotflow init again, "
            f"or add {agent_setup.PROFILES_KEY}={agent_setup.AGENT_PROFILE} to {options.env_file}."
        )
    if {drafting.how, execution.how} & {"rotated", "regenerated"}:
        notes.append(
            "  Restart with `docker compose up -d`, so JotFlow and the agent containers "
            "use the new secret."
        )
    return AgentFiles(changes, env_updates, notes)


def _saved_agent_message(options: InitOptions) -> str:
    names = f"{options.agent_file} and {options.agent_exec_file}"
    if on_windows_drive(options.agent_file):
        return (
            f"Saved {names}. They are on a Windows drive, so the folder's Windows permissions, "
            "not JotFlow, decide who can read them."
        )
    return f"Saved {names}. Only your user can read them."


def run_init(console: Console, api: SlackApi, options: InitOptions) -> int:
    """Run the setup session. Returns a process exit code."""
    console.say(INTRO)
    console.say()
    existing = read_env(options.env_file)

    # Same order as the steps: the app-level token, then the bot token.
    app_token = existing.get(APP_TOKEN_KEY, "")
    keep_app_token = bool(app_token) and _existing_app_token(console, api, app_token)
    identity: BotIdentity | None = None
    bot_token = existing.get(BOT_TOKEN_KEY, "")
    if bot_token:
        identity = _existing_bot(console, api, bot_token)

    if identity is None:
        _create_app_steps(console, options)
    if not keep_app_token:
        _step(console, 3)
        _explain(console, EXPLAIN_APP_TOKEN)
        app_token, _ = _paste_until_valid(
            console,
            "Paste the app-level token (xapp-, hidden)",
            lambda t: check_app_token(api, t),
        )
        console.say("  App-level token works: Socket Mode can connect.")
        console.say()
    if identity is None:
        _step(console, 4)
        _explain(console, EXPLAIN_BOT_TOKEN)
        bot_token, new_identity = _paste_until_valid(
            console, "Paste the bot token (xoxb-, hidden)", lambda t: check_bot_token(api, t)
        )
        identity = new_identity
        console.say(f"  Bot token works: {_describe(identity)}.")

    if not identity.scopes_verified:
        console.say(
            "  Slack did not report the bot token's scopes, so they could not be checked. "
            "If JotFlow later reports a missing_scope error, reinstall the app from its manifest."
        )

    policy = _policy_step(console, options)
    agent_choice = _ask_agent(console, options, _chosen_preset(options, policy))
    compose_plan = _plan_compose(console, options)

    console.say()
    updates = {BOT_TOKEN_KEY: bot_token, APP_TOKEN_KEY: app_token}
    agent_files = (
        None if agent_choice is None else _plan_agent_files(options, agent_choice, existing)
    )
    changes: list[EnvChange] = []
    if agent_files is not None:
        updates |= agent_files.env_updates
        changes += agent_files.changes
    compose_file = existing.get(COMPOSE_FILE_KEY)
    if compose_file is None:
        updates[COMPOSE_FILE_KEY] = COMPOSE_FILE_VALUE
    if options.windows and COMPOSE_SEPARATOR_KEY not in existing:
        updates[COMPOSE_SEPARATOR_KEY] = COMPOSE_SEPARATOR_VALUE
    # One step: if any file cannot be written, none of them changes, so a
    # rotation never leaves .env and an agent file holding different secrets.
    write_envs([*changes, (options.env_file, updates, "")])
    for note in agent_files.notes if agent_files is not None else []:
        console.say(note)
    console.say(_saved_tokens_message(options.env_file))
    if COMPOSE_FILE_KEY in updates:
        console.say(
            f"  Also set {COMPOSE_FILE_KEY} there, so `docker compose up -d` "
            "hands jotflow.toml to JotFlow."
        )
    if COMPOSE_SEPARATOR_KEY in updates:
        console.say(
            f"  And {COMPOSE_SEPARATOR_KEY}={COMPOSE_SEPARATOR_VALUE}, which Docker needs on "
            "Windows to read that line."
        )
    if compose_file is not None and compose_file != COMPOSE_FILE_VALUE:
        console.say(
            f"  Left your {COMPOSE_FILE_KEY}={compose_file} as it is. For Docker to mount "
            f"jotflow.toml it should include docker-compose.policy.yml."
        )
    if policy is not None:
        _save_policy(console, options.config_file, policy)
    _write_compose(console, compose_plan)
    console.say(f"Connected to Slack as {_describe(identity)}.")
    console.say()
    bot = f"@{identity.bot_user}" if identity.bot_user else "the bot"
    console.say("Next:")
    console.say(
        f"  1. Invite {bot} to each channel JotFlow should watch with /invite {bot}. "
        "It never joins channels by itself."
    )
    console.say("  2. Check the setup with: jotflow check")
    console.say("  3. Start JotFlow with: docker compose up -d")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register this command's options, with the intro as the help text."""
    parser.description = INTRO
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="where to save the Slack tokens (default: .env)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="where to save the policy file (default: $JOTFLOW_CONFIG or jotflow.toml)",
    )
    parser.add_argument(
        "--rotate-runner-secret",
        action="store_true",
        help="replace the drafting runner's secret in .env and agent.env",
    )
    parser.add_argument(
        "--rotate-exec-secret",
        action="store_true",
        help="replace the execution runner's secret in .env and agent-exec.env",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path(DEFAULT_MANIFEST_OUT),
        help=f"where to save the rendered Slack manifest (default: {DEFAULT_MANIFEST_OUT})",
    )


def run(
    args: argparse.Namespace,
    *,
    console: Console | None = None,
    api: SlackApi | None = None,
) -> int:
    if console is None:
        if not sys.stdin.isatty():
            print("jotflow init is interactive; run it in a terminal.", file=sys.stderr)
            return 2
        console = Console()
    options = InitOptions(
        env_file=args.env_file,
        manifest_out=args.manifest_out,
        config_file=getattr(args, "config", None) or _default_config_file(),
        rotate_runner_secret=getattr(args, "rotate_runner_secret", False),
        rotate_exec_secret=getattr(args, "rotate_exec_secret", False),
    )
    try:
        return run_init(console, api or HttpSlackApi(), options)
    except SetupAborted:
        console.say()
        console.say(
            f"Setup stopped. {options.env_file} and {options.config_file} were not changed."
        )
        return 1
