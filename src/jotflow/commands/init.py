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
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from jotflow.envfile import read_env, write_env
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
    BotNameError,
    render_manifest_json,
    validate_bot_name,
)

BOT_TOKEN_KEY = "JOTFLOW_SLACK_BOT_TOKEN"
APP_TOKEN_KEY = "JOTFLOW_SLACK_APP_TOKEN"

CREATE_APP_URL = "https://api.slack.com/apps"
DEFAULT_MANIFEST_OUT = "slack-app-manifest.json"
MAX_ATTEMPTS = 5


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


@dataclass(frozen=True)
class InitOptions:
    env_file: Path = Path(".env")
    manifest_out: Path = Path(DEFAULT_MANIFEST_OUT)


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
    name = console.ask("What should the bot be called in Slack?", DEFAULT_BOT_NAME, _bot_name)
    manifest = render_manifest_json(name)
    options.manifest_out.write_text(manifest, encoding="utf-8")
    console.say()
    console.say("Create the Slack app. Open this link, pick your workspace, and click Create:")
    console.say()
    console.say(f"  {create_app_link(manifest)}")
    console.say()
    console.say(
        f"If the link does not open, choose Create New App, then From a manifest, at "
        f"{CREATE_APP_URL} and paste the contents of {options.manifest_out}."
    )
    console.say()
    console.say("Then, in the new app's settings:")
    console.say(
        "  1. Basic Information, App-Level Tokens: generate a token with the "
        "connections:write scope. It starts with xapp-."
    )
    console.say(
        "  2. Install App: click Install to Workspace, then Allow. "
        "Copy the Bot User OAuth Token. It starts with xoxb-."
    )
    console.say()


def run_init(console: Console, api: SlackApi, options: InitOptions) -> int:
    """Run the setup session. Returns a process exit code."""
    console.say("JotFlow setup. Press Ctrl-D at any prompt to stop.")
    console.say("Your tokens are saved only after both have been checked.")
    console.say()
    existing = read_env(options.env_file)

    identity: BotIdentity | None = None
    bot_token = existing.get(BOT_TOKEN_KEY, "")
    if bot_token:
        identity = _existing_bot(console, api, bot_token)
    app_token = existing.get(APP_TOKEN_KEY, "")
    keep_app_token = bool(app_token) and _existing_app_token(console, api, app_token)

    if identity is None:
        _create_app_steps(console, options)
        bot_token, new_identity = _paste_until_valid(
            console, "Paste the bot token (xoxb-, hidden)", lambda t: check_bot_token(api, t)
        )
        identity = new_identity
        console.say(f"  Bot token works: {_describe(identity)}.")
    if not keep_app_token:
        app_token, _ = _paste_until_valid(
            console,
            "Paste the app-level token (xapp-, hidden)",
            lambda t: check_app_token(api, t),
        )
        console.say("  App-level token works: Socket Mode can connect.")

    if not identity.scopes_verified:
        console.say(
            "  Slack did not report the bot token's scopes, so they could not be checked. "
            "If JotFlow later reports a missing_scope error, reinstall the app from its manifest."
        )

    write_env(options.env_file, {BOT_TOKEN_KEY: bot_token, APP_TOKEN_KEY: app_token})
    console.say()
    console.say(f"Saved both tokens to {options.env_file}. Only your user can read that file.")
    console.say(f"Connected to Slack as {_describe(identity)}.")
    console.say()
    bot = f"@{identity.bot_user}" if identity.bot_user else "the bot"
    console.say(
        f"Next: invite {bot} to each channel JotFlow should watch with /invite {bot}. "
        "It never joins channels by itself."
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register this command's options."""
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="where to save the Slack tokens (default: .env)",
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
    options = InitOptions(env_file=args.env_file, manifest_out=args.manifest_out)
    try:
        return run_init(console, api or HttpSlackApi(), options)
    except SetupAborted:
        console.say()
        console.say(f"Setup stopped. {options.env_file} was not changed.")
        return 1
