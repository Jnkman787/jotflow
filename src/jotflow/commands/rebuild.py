"""`jotflow rebuild`: restore the store's index from JotFlow's own Slack replies (#38).

Run it with JotFlow stopped, from the JotFlow folder::

    docker compose stop jotflow
    docker compose run --rm --no-deps jotflow jotflow rebuild
    docker compose start jotflow

It refuses a store that already has topics in a configured channel, unless
``--merge`` is given. With ``--merge`` it adds only what is missing and never
overwrites. ``--dry-run`` reads Slack and prints what it would restore, writing
nothing. See :mod:`jotflow.rebuild` for what is restored and what can't be.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.commands.run import SettingsError, load_settings
from jotflow.orchestrator.health import MAX_AGE_SECONDS, heartbeat_age, heartbeat_path
from jotflow.orchestrator.ingest import BotIdentity
from jotflow.orchestrator.packet import SlackFetchError
from jotflow.policy import Policy, PolicyError, load_policy
from jotflow.rebuild import ChannelPlan, RebuildSlack, Restored, apply, collect
from jotflow.store import SqliteStore, Store, StoreError
from jotflow.store.lock import UNSUPPORTED, LockUnsupportedError, StoreBusyError

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_SLACK = 3
EXIT_STORE = 4
_SINCE = re.compile(r"(?P<days>[0-9]{1,5})d")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        default="all",
        metavar="DAYS",
        help="How far back to read, such as 90d, or all (the default)",
    )
    parser.add_argument(
        "--channel",
        action="append",
        metavar="CHANNEL_ID",
        help="Rebuild only this configured channel. Repeat for more",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Add to a store that already has topics, never overwriting what's there",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="On a filesystem that can't lock, run without the check that JotFlow isn't "
        "running. Stop JotFlow yourself first. A running JotFlow is still refused where "
        "locking works",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Slack and print what would be restored, writing nothing",
    )


class BoltRebuildSlack:
    """:class:`RebuildSlack` over slack-sdk, waiting out rate limits as Slack asks."""

    def __init__(self, client: AsyncWebClient) -> None:
        self._client = client

    async def history(
        self, channel: str, *, cursor: str | None, oldest: str | None, limit: int
    ) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {"channel": channel, "cursor": cursor, "limit": limit}
        if oldest is not None:
            kwargs["oldest"] = oldest
        response = await self._client.conversations_history(**kwargs)
        return response.data if isinstance(response.data, dict) else {}

    async def replies(
        self, channel: str, ts: str, *, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        response = await self._client.conversations_replies(
            channel=channel, ts=ts, cursor=cursor, limit=limit
        )
        return response.data if isinstance(response.data, dict) else {}


def _oldest(since: str, now: float) -> str | None:
    if since == "all":
        return None
    match = _SINCE.fullmatch(since)
    if match is None:
        raise ValueError("give --since as a number of days, such as 90d, or all")
    return f"{now - int(match['days']) * 86400:.6f}"


async def _refuse_non_empty(store: Store, channels: list[str]) -> str | None:
    for channel in channels:
        if await store.channel_topics(channel):
            return channel
    return None


async def rebuild(
    store: Store,
    policy: Policy,
    slack: RebuildSlack,
    bot: BotIdentity,
    channels: list[str],
    *,
    oldest: str | None,
    merge: bool,
    dry_run: bool,
    say: Any = print,
) -> list[tuple[ChannelPlan, Restored | None]]:
    """Plan each channel, then write it unless this is a dry run. Raises nothing new."""
    if not merge and not dry_run:
        busy = await _refuse_non_empty(store, channels)
        if busy is not None:
            raise RefusedError(
                f"the store already has topics in {busy}; "
                "use --merge to add only what's missing, or start from an empty database"
            )
    results: list[tuple[ChannelPlan, Restored | None]] = []
    for channel in channels:
        say(f"{channel}: reading history")
        repos = [entry.repo for entry in policy.channels[channel].repos]
        plan = await collect(slack, channel, bot, oldest=oldest, repos=repos, progress=say)
        restored = None if dry_run else await apply(store, policy, plan, bot)
        results.append((plan, restored))
        say(_summary(plan, restored, windowed=oldest is not None))
    return results


class RefusedError(Exception):
    """Rebuild refuses to run; the message says why and what to do."""


def _summary(plan: ChannelPlan, restored: Restored | None, *, windowed: bool = False) -> str:
    head = (
        f"{plan.channel_id}: {plan.messages_read} messages read, "
        f"{len(plan.threads)} threads with issues, {plan.issue_count} issues"
    )
    # A mode set before --since is outside the window: the channel falls back to
    # propose, so a paused or auto channel must be checked by hand.
    unsure = (
        f"; no mode announcement since --since, "
        f"so check {plan.channel_id}'s mode with /jotflow mode"
        if windowed and plan.mode is None
        else ""
    )
    if restored is None:
        return head + " (dry run, nothing written)" + unsure
    parts = [
        f"{restored.topics} topics restored",
        f"{restored.linked} messages linked",
        f"{restored.chatter} skip: messages marked",
    ]
    if restored.reused_topics or restored.kept:
        parts.append(f"{restored.reused_topics} topics and {restored.kept} messages already there")
    if restored.mode:
        parts.append(f"mode {plan.mode.value if plan.mode else ''}")
    if restored.guide:
        parts.append("guide noted")
    if plan.unknown_repos:
        parts.append(f"{plan.unknown_repos} replies for repos not in jotflow.toml skipped")
    return f"{head}; " + ", ".join(parts) + unsure


async def _run(args: argparse.Namespace, policy: Policy, channels: list[str]) -> int:
    settings = load_settings()
    client = AsyncWebClient(token=settings.slack_bot_token.get_secret_value())
    try:
        auth = await client.auth_test()
    except SlackApiError as exc:
        print(
            f"jotflow rebuild: Slack refused the bot token ({exc.response.get('error')})",
            file=sys.stderr,
        )
        return EXIT_SLACK
    bot = BotIdentity(user_id=str(auth.get("user_id") or ""), bot_id=auth.get("bot_id"))
    try:
        # --no-lock still takes the lock where it can, so a running JotFlow is still
        # refused. It only allows running unlocked where the filesystem can't lock.
        store = await SqliteStore.open(
            settings.database_path, exclusive=True, unlocked_ok=args.no_lock
        )
    except StoreBusyError as exc:
        print(f"jotflow rebuild: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except LockUnsupportedError:
        print(
            "jotflow rebuild: refusing: the database's filesystem doesn't support locking, "
            "so rebuild can't check that JotFlow is stopped. Stop it first "
            "(docker compose stop jotflow), then rerun with --no-lock",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not store.locked:
        print(f"jotflow rebuild: running without the safety check: {UNSUPPORTED}", file=sys.stderr)
    try:
        await rebuild(
            store,
            policy,
            BoltRebuildSlack(client),
            bot,
            channels,
            oldest=_oldest(args.since, time.time()),
            merge=args.merge,
            dry_run=args.dry_run,
        )
    except RefusedError as exc:
        print(f"jotflow rebuild: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except SlackFetchError as exc:
        print(
            f"jotflow rebuild: stopped: {exc}. What was restored is kept; run it again "
            "with --merge to continue",
            file=sys.stderr,
        )
        return EXIT_SLACK
    finally:
        await store.close()
    print("Done. Start JotFlow again with: docker compose start jotflow")
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    age = heartbeat_age(heartbeat_path(), time.time())
    if age is not None and age <= MAX_AGE_SECONDS:
        print(
            "jotflow rebuild: refusing: JotFlow is running here. Stop it first: "
            "docker compose stop jotflow",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        _oldest(args.since, time.time())
        settings = load_settings()
        policy = load_policy(settings.config_path)
    except ValueError as exc:
        print(f"jotflow rebuild: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (SettingsError, PolicyError) as exc:
        print(f"jotflow rebuild: configuration problem in {exc}", file=sys.stderr)
        return EXIT_REFUSED
    channels = list(policy.channels)
    if args.channel:
        unknown = [c for c in args.channel if c not in policy.channels]
        if unknown:
            print(
                f"jotflow rebuild: refusing: {', '.join(unknown)} isn't in jotflow.toml",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        channels = list(dict.fromkeys(args.channel))
    try:
        return asyncio.run(_run(args, policy, channels))
    except (StoreError, OSError, sqlite3.Error) as exc:
        print(
            f"jotflow rebuild: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_STORE
