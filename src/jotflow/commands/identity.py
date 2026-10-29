"""`jotflow identity`: whoever runs JotFlow manages GitHub links on the host.

`jotflow identity link` loads links as an admin (#305): one pair, or a CSV file of
``slack_member_id,github_login``. Each login is looked up through the drafting
runner first, and its GitHub account id is stored with the link, so a self-add can
check later that the login still names the same account. Every line is checked
before anything is written. A login someone else has is refused, unless
``--replace``: then their link is removed, recorded as by ``operator``, and they're
told privately. Only counts are printed, never logins::

    docker compose exec jotflow jotflow identity link U0123ABCD octo-example
    docker compose exec jotflow jotflow identity link --file /data/links.csv

`jotflow identity unlink`: whoever runs JotFlow removes someone's GitHub link (#173).

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
import csv
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from jotflow.commands.run import SettingsError, load_settings
from jotflow.orchestrator.identity import (
    SlackNote,
    clean_login,
    member_id,
    removal_note,
    remove_link,
)
from jotflow.orchestrator.launch import GithubBusy, RunnerClient, RunnerError
from jotflow.store import OPERATOR_REMOVER, ConflictError, SqliteStore, StoreError

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_STORE = 4


# The most lines one file may hold.
MAX_LINKS = 5000
HEADER = ("slack_member_id", "github_login")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", metavar="<action>")
    link = sub.add_parser("link", help="Link Slack members to GitHub logins, as an admin")
    link.add_argument("member_id", nargs="?", help="A Slack member ID, such as U0123ABCD")
    link.add_argument("github_login", nargs="?", help="Their GitHub login")
    link.add_argument(
        "--file", type=Path, help="A CSV file with a slack_member_id,github_login header"
    )
    link.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Take a login from whoever has it now, or change a member's own link, and tell "
            "them privately. A run that stops partway is safe to run again"
        ),
    )
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


class LinkRefused(Exception):
    """A line can't be linked. The message names the line, never a login."""


@dataclass
class LinkCounts:
    linked: int = 0
    unchanged: int = 0
    kept_verified: int = 0
    replaced: int = 0
    told: int = 0
    # (Slack id, the note they get), for everyone whose link this run changed.
    notes: list[tuple[str, str]] = field(default_factory=list)


def read_pairs(args: argparse.Namespace) -> list[tuple[str, str]]:
    """The pairs to link, from the command line or the file, all checked, or raise."""
    if args.file is not None:
        if args.member_id or args.github_login:
            raise LinkRefused("give either --file or one member ID and login, not both")
        # utf-8-sig: a spreadsheet's export often starts with a byte-order mark.
        rows = list(csv.reader(args.file.read_text(encoding="utf-8-sig").splitlines()))
        if not rows or tuple(cell.strip() for cell in rows[0]) != HEADER:
            raise LinkRefused("the file's first line must be: slack_member_id,github_login")
        raw = [(n, row) for n, row in enumerate(rows[1:], start=2) if any(c.strip() for c in row)]
    elif args.member_id and args.github_login:
        raw = [(1, [args.member_id, args.github_login])]
    else:
        raise LinkRefused("give a member ID and a GitHub login, or --file")
    if len(raw) > MAX_LINKS:
        raise LinkRefused(f"at most {MAX_LINKS} links at a time")
    pairs: list[tuple[str, str]] = []
    users: set[str] = set()
    logins: set[str] = set()
    for number, row in raw:
        where = f"line {number}" if args.file is not None else "that pair"
        if len(row) != 2:
            raise LinkRefused(f"{where} doesn't have exactly two columns")
        user, login = member_id(row[0].strip()), clean_login(row[1])
        if user is None:
            raise LinkRefused(f"{where}: the first column isn't a Slack member ID")
        if login is None or login.endswith("[bot]"):
            raise LinkRefused(f"{where}: the second column isn't a person's GitHub login")
        if user in users or login.casefold() in logins:
            raise LinkRefused(f"{where} repeats a member ID or a login")
        users.add(user)
        logins.add(login.casefold())
        pairs.append((user, login))
    return pairs


@dataclass(frozen=True)
class _Plan:
    user: str
    login: str
    account_id: int
    action: str  # link, unchanged, keep, or replace_own
    old_login: str | None = None


def changed_note(old: str, new: str) -> str:
    """What someone is told when an admin changes their link to another login (#305)."""
    return (
        f"Whoever runs JotFlow changed the GitHub login linked to your Slack account from "
        f"{old} to {new}. Cards show it as linked by an admin. If that's wrong, tell them, or "
        "link your own with /jotflow link your-github-login."
    )


async def load_links(
    store: SqliteStore,
    runner: RunnerClient,
    notifier: SlackNote,
    pairs: list[tuple[str, str]],
    *,
    replace: bool,
) -> LinkCounts:
    """Look every login up and plan every line first, refusing before any write if one
    fails. Then link them one by one.

    For someone who already has a link (#305 review):
    - the same login, with a matching or no account id: a verified link is kept as it
      is, its id filled in if missing; an unverified one becomes the admin's
    - the same login with another account id: refused, since the login was renamed or
      reused
    - another login: refused without ``--replace``; with it, changed, recorded as by
      ``operator``, and the person is told, naming both logins

    A run that stops partway can be run again: lines already done count as unchanged.
    """
    plans: list[_Plan] = []
    for number, (user, login) in enumerate(pairs, start=1):
        try:
            found = await runner.account(login)
        except GithubBusy:
            raise LinkRefused("GitHub couldn't be asked right now; nothing was linked") from None
        except RunnerError as exc:
            raise LinkRefused(f"the drafting runner couldn't check logins ({exc})") from None
        if found.account == "missing" or found.id is None or found.login is None:
            raise LinkRefused(f"link {number}: there's no GitHub account with that login")
        if found.account != "user":
            raise LinkRefused(f"link {number}: that login is an organization, not a person")
        holder = await store.identity_for_login(found.login)
        if holder is not None and holder.slack_user_id != user and not replace:
            raise LinkRefused(
                f"link {number}: someone else has linked that login; pass --replace to take it"
            )
        current = await store.get_identity(user)
        plans.append(_plan(number, user, found.login, found.id, current, replace))
    counts = LinkCounts()
    for plan in plans:
        if plan.action == "unchanged":
            counts.unchanged += 1
            continue
        if plan.action == "keep":
            await store.fill_github_id(plan.user, plan.account_id)
            counts.kept_verified += 1
            continue
        if plan.action == "replace_own":
            await store.remove_identity(plan.user, removed_by=OPERATOR_REMOVER)
        try:
            done = await store.admin_link(plan.user, plan.login, plan.account_id, replace=replace)
        except ConflictError:
            raise LinkRefused(
                f"a login was linked by someone else meanwhile, after {counts.linked} were "
                "linked; run it again to finish"
            ) from None
        counts.linked += 1
        if plan.action == "replace_own" and plan.old_login is not None:
            counts.replaced += 1
            counts.notes.append((plan.user, changed_note(plan.old_login, plan.login)))
        if done.displaced is not None:
            counts.replaced += 1
            counts.notes.append((done.displaced, removal_note(None)))
    for user, text in counts.notes:
        try:
            await notifier.tell(user, text)
            counts.told += 1
        except Exception:  # told or not, the count says so
            pass
    return counts


def _plan(
    number: int, user: str, login: str, account_id: int, current: Any, replace: bool
) -> _Plan:
    if current is None:
        return _Plan(user, login, account_id, "link")
    if current.github_login.casefold() == login.casefold():
        if current.github_id is not None and current.github_id != account_id:
            raise LinkRefused(
                f"link {number}: that login now names a different GitHub account than the "
                "one already linked; it was renamed or reused, so nothing was linked"
            )
        if current.verified:
            return _Plan(user, login, account_id, "keep")  # never downgraded to an admin's
        if current.linked_by == "admin" and current.github_id == account_id:
            return _Plan(user, login, account_id, "unchanged")
        return _Plan(user, login, account_id, "link")
    if not replace:
        raise LinkRefused(
            f"link {number}: that member already has a different GitHub login linked; "
            "pass --replace to change it, and they'll be told"
        )
    return _Plan(user, login, account_id, "replace_own", old_login=current.github_login)


async def _link(args: argparse.Namespace, pairs: list[tuple[str, str]]) -> LinkCounts:
    settings = load_settings()
    store = await SqliteStore.open(settings.database_path)
    runner = RunnerClient.from_settings(settings)
    try:
        client = AsyncWebClient(token=settings.slack_bot_token.get_secret_value())
        return await load_links(store, runner, SlackNote(client), pairs, replace=args.replace)
    finally:
        await store.close()


def run_link(args: argparse.Namespace) -> int:
    try:
        pairs = read_pairs(args)
        counts = asyncio.run(_link(args, pairs))
    except LinkRefused as exc:
        print(f"jotflow identity link: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, UnicodeDecodeError) as exc:
        print(f"jotflow identity link: can't read the file ({type(exc).__name__})", file=sys.stderr)
        return EXIT_REFUSED
    except SettingsError as exc:
        print(f"jotflow identity link: configuration problem in {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (StoreError, sqlite3.Error) as exc:
        print(
            f"jotflow identity link: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_STORE
    print(
        f"linked {counts.linked}, unchanged {counts.unchanged}, "
        f"already verified and kept {counts.kept_verified}, replaced {counts.replaced}"
        + (f" (told {counts.told} of them privately)" if counts.replaced else "")
    )
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if getattr(args, "action", None) is None:
        print(getattr(args, "identity_usage", "") + "choose link or unlink", file=sys.stderr)
        return EXIT_REFUSED
    if args.action == "link":
        return run_link(args)
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
