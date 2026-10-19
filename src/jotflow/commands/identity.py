"""`jotflow identity unlink`: whoever runs JotFlow removes someone's GitHub link (#173).

For a login claimed by the wrong person, when no approver or identity admin is
set, or none is around. It runs on the host, inside the JotFlow container::

    docker compose exec jotflow jotflow identity unlink U0123ABCD

It takes the Slack member ID, never a name. The person is told privately in
the app's Messages tab, and the store records the removal as made by
``operator``, with no login and no text.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys

from slack_sdk.web.async_client import AsyncWebClient

from jotflow.commands.run import SettingsError, load_settings
from jotflow.orchestrator.identity import SlackNote, member_id, remove_link
from jotflow.store import OPERATOR_REMOVER, SqliteStore, StoreError

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_STORE = 4


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", metavar="<action>")
    unlink = sub.add_parser("unlink", help="Remove someone's GitHub link and tell them privately")
    unlink.add_argument("member_id", help="Their Slack member ID, such as U0123ABCD")
    parser.set_defaults(identity_usage=parser.format_usage())


async def unlink(database_path: str, bot_token: str, user_id: str) -> tuple[bool, bool]:
    store = await SqliteStore.open(database_path)
    try:
        client = AsyncWebClient(token=bot_token)
        return await remove_link(
            store, SlackNote(client), user_id, removed_by=OPERATOR_REMOVER, by_name=None
        )
    finally:
        await store.close()


def run(args: argparse.Namespace) -> int:
    if getattr(args, "action", None) is None:
        print(getattr(args, "identity_usage", "") + "choose unlink", file=sys.stderr)
        return EXIT_REFUSED
    target = member_id(args.member_id)
    if target is None:
        print(
            "jotflow identity unlink: refusing: give a Slack member ID, such as U0123ABCD",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        settings = load_settings()
        removed, told = asyncio.run(
            unlink(settings.database_path, settings.slack_bot_token.get_secret_value(), target)
        )
    except SettingsError as exc:
        print(f"jotflow identity unlink: configuration problem in {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (StoreError, OSError, sqlite3.Error) as exc:
        print(
            f"jotflow identity unlink: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_STORE
    if not removed:
        print(f"{target} has no linked GitHub login")
        return EXIT_OK
    if told:
        print(f"unlinked {target}'s GitHub login; they were told privately")
    else:
        print(f"unlinked {target}'s GitHub login; JotFlow couldn't message them, so tell them")
    return EXIT_OK
