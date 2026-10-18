"""`jotflow check`: verify configuration, and with ``--live`` a real Slack round trip.

Without flags the check is offline: it loads settings and ``jotflow.toml``
exactly as ``jotflow run`` does and reports what it found.

``--live`` posts one marked test message to a configured channel and deletes
it again; see :mod:`jotflow.live_check`. It is for a person verifying an
install by hand, so it refuses to run unless ``JOTFLOW_LIVE_CHECK=1`` is set
in the environment for that run, and it refuses outright when ``CI`` is set.
JotFlow reads the opt-in from the process environment, never from ``.env``,
and refuses if ``.env`` contains it. That does not stop every way of leaving
it on: Compose's ``env_file`` turns ``.env`` lines into the container's
environment, and a shell profile can export it. So the live check refuses
inside JotFlow's own image, which sets ``JOTFLOW_IMAGE``, and runs on the host
instead; plain ``jotflow check`` still works in the image. The marker is a
guard against accidents, not a hard boundary: ``env_file`` or ``-e`` can set
it to a false value. The opt-in belongs on the command line, and an
interactive run also asks before it posts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from jotflow.commands.run import (
    SettingsError,
    configure_logging,
    load_settings,
)
from jotflow.config import Settings
from jotflow.envfile import read_env
from jotflow.live_check import DEFAULT_TIMEOUT, BoltLiveSlack, LiveSlack, run_live_check
from jotflow.policy import Policy, PolicyError, load_policy

EXIT_OK = 0
EXIT_REFUSED = 2

OPT_IN = "JOTFLOW_LIVE_CHECK"
CHANNEL_VAR = "JOTFLOW_LIVE_CHECK_CHANNEL"
IMAGE_VAR = "JOTFLOW_IMAGE"
EXIT_CANCELLED = 130


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register this command's options."""
    parser.add_argument(
        "--live",
        action="store_true",
        help=f"Also run the live Slack round trip. Requires {OPT_IN}=1.",
    )
    parser.add_argument(
        "--channel",
        metavar="CHANNEL_ID",
        help=f"Configured channel for the test message (default: ${CHANNEL_VAR}, "
        "or the only configured channel)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Seconds to wait for the test message's event (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask before posting, even in a terminal",
    )


class Refusal(Exception):
    """The live check will not run. The message says why."""


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "0", "false", "no"}


def check_opt_in(environ: Mapping[str, str], dotenv: Mapping[str, str]) -> None:
    if _truthy(environ.get(IMAGE_VAR)):
        raise Refusal(
            f"this is JotFlow's container image, where Compose passes .env into the "
            f"environment; run the live check on the host: {OPT_IN}=1 make live-check"
        )
    if _truthy(environ.get("CI")):
        raise Refusal("the live check never runs in CI; CI must not hold Slack tokens")
    if OPT_IN in dotenv:
        raise Refusal(
            f"remove the {OPT_IN} line from .env and set it only for this one run: "
            f"{OPT_IN}=1 jotflow check --live"
        )
    if environ.get(OPT_IN) != "1":
        raise Refusal(
            f"the live check posts to Slack, so it runs only when you opt in for this run: "
            f"{OPT_IN}=1 jotflow check --live"
        )


def choose_channel(requested: str | None, environ: Mapping[str, str], policy: Policy) -> str:
    """The channel to post to. It must be one ``jotflow.toml`` lists."""
    channel = requested or environ.get(CHANNEL_VAR) or None
    configured = list(policy.channels)
    if channel is None:
        if len(configured) == 1:
            return configured[0]
        raise Refusal(
            f"jotflow.toml lists {len(configured)} channels; choose one with --channel "
            f"or {CHANNEL_VAR}: {', '.join(configured)}"
        )
    if channel not in policy.channels:
        raise Refusal(
            f"channel {channel} is not in jotflow.toml; the live check only posts to a "
            "configured channel"
        )
    return channel


def confirm(
    channel: str,
    *,
    interactive: bool,
    ask: Callable[[str], str] | None = None,
) -> bool:
    """In a terminal, ask before posting. Scripts and ``--yes`` are not asked."""
    if not interactive:
        return True
    try:
        answer = (ask or input)(f"Post one test message to channel {channel}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _load() -> tuple[Settings, Policy]:
    settings = load_settings()
    return settings, load_policy(settings.config_path)


def make_slack(settings: Settings) -> LiveSlack:
    return BoltLiveSlack(
        settings.slack_bot_token.get_secret_value(),
        settings.slack_app_token.get_secret_value(),
    )


def run(args: argparse.Namespace) -> int:
    configure_logging("warning")
    try:
        settings, policy = _load()
    except (SettingsError, PolicyError) as exc:
        print(f"jotflow check: configuration problem in {exc}", file=sys.stderr)
        return EXIT_REFUSED
    count = len(policy.channels)
    print(f"ok    configuration: {count} channel(s) in {settings.config_path}")
    if not getattr(args, "live", False):
        return EXIT_OK

    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
    try:
        if timeout <= 0:
            raise Refusal("--timeout must be more than 0 seconds")
        check_opt_in(os.environ, read_env(Path(".env")))
        channel = choose_channel(getattr(args, "channel", None), os.environ, policy)
    except Refusal as exc:
        print(f"jotflow check: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"Live check: posting one test message to channel {channel}, then deleting it.")
    print("Stop any running JotFlow first: Slack sends each event to only one connection.")
    interactive = not getattr(args, "yes", False) and sys.stdin.isatty()
    try:
        go_ahead = confirm(channel, interactive=interactive)
    except KeyboardInterrupt:
        print("\njotflow check: cancelled, nothing posted", file=sys.stderr)
        return EXIT_CANCELLED
    if not go_ahead:
        print("jotflow check: not posting; nothing was sent", file=sys.stderr)
        return EXIT_REFUSED
    return asyncio.run(
        run_live_check(make_slack(settings), channel, out=sys.stdout, timeout=timeout)
    )
