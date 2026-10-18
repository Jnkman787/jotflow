"""Command line entry point.

Each subcommand lives in its own module under ``jotflow.commands`` and exposes
``add_arguments(parser)`` and ``run(args) -> int``. Adding a subcommand means
adding a module and one line to ``COMMANDS``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from types import ModuleType

from jotflow import __version__
from jotflow.commands import check as check_command
from jotflow.commands import fixture as fixture_command
from jotflow.commands import init as init_command
from jotflow.commands import replay as replay_command
from jotflow.commands import run as run_command
from jotflow.commands import runner as runner_command

COMMANDS: dict[str, tuple[ModuleType, str]] = {
    "init": (init_command, "Guided setup: write jotflow.toml and connect the Slack app"),
    "run": (run_command, "Connect to Slack and start JotFlow"),
    "check": (
        check_command,
        "Check configuration; with --live, verify a real Slack round trip",
    ),
    "replay": (
        replay_command,
        "Run a saved thread through JotFlow offline and compare it with its expected outcome",
    ),
    "runner": (runner_command, "Inside the agent container: start agent runs for JotFlow"),
    "execute-fixture": (
        fixture_command,
        "Queue an execution run for a hand-approved proposal, to check an install end to end",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jotflow", description="Turn team chat into GitHub issues."
    )
    parser.add_argument("--version", action="version", version=f"jotflow {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name, (module, help_text) in COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        module.add_arguments(cmd)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    module, _ = COMMANDS[args.command]
    result: int = module.run(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
