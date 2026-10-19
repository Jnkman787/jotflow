"""The Slack side of ingest: identity, channel membership, and event listeners."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import aiohttp
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.models import ChannelMode
from jotflow.orchestrator.help import channel_help, help_text
from jotflow.orchestrator.ingest import BotIdentity, Decision, EventFilter, log_decision
from jotflow.policy import Policy
from jotflow.slack_manifest import SLASH_COMMAND

log = logging.getLogger("jotflow.slack")


class SlackSetupError(Exception):
    """Slack was unreachable, or rejected JotFlow's credentials or scopes, at startup."""


NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError, OSError)


def _unreachable(exc: BaseException) -> SlackSetupError:
    return SlackSetupError(f"could not reach Slack ({type(exc).__name__}); check the network")


async def fetch_identity(client: AsyncWebClient) -> tuple[BotIdentity, str]:
    """The bot's ids, and its display name for the ``/invite`` hint, from ``auth.test``."""
    try:
        response = await client.auth_test()
    except SlackApiError as exc:
        raise SlackSetupError(
            f"Slack rejected the bot token ({exc.response.get('error', 'unknown error')}); "
            "check JOTFLOW_SLACK_BOT_TOKEN"
        ) from None
    except NETWORK_ERRORS as exc:
        raise _unreachable(exc) from None
    user_id = response.get("user_id")
    if not isinstance(user_id, str):
        raise SlackSetupError("auth.test returned no bot user id; is this a bot token?")
    bot_id = response.get("bot_id")
    name = response.get("user")
    return (
        BotIdentity(user_id=user_id, bot_id=bot_id if isinstance(bot_id, str) else None),
        name if isinstance(name, str) else user_id,
    )


async def missing_channels(client: AsyncWebClient, channel_ids: Iterable[str]) -> list[str]:
    """Configured channels the bot is not a member of, in the order given.

    A private channel the bot was never invited to is invisible to it, so
    ``channel_not_found`` counts as not a member.
    """
    missing: list[str] = []
    for channel_id in channel_ids:
        try:
            response = await client.conversations_info(channel=channel_id)
        except SlackApiError as exc:
            error = exc.response.get("error")
            if error == "channel_not_found":
                missing.append(channel_id)
                continue
            raise SlackSetupError(
                f"could not look up channel {channel_id} ({error}); "
                "the app needs the channels:read and groups:read scopes"
            ) from None
        except NETWORK_ERRORS as exc:
            raise _unreachable(exc) from None
        channel = response.get("channel")
        if not isinstance(channel, dict) or not channel.get("is_member"):
            missing.append(channel_id)
    return missing


def report_missing(missing: list[str], bot_name: str) -> None:
    for channel_id in missing:
        log.warning(
            "not a member of channel %s; invite the bot there with /invite @%s",
            channel_id,
            bot_name,
        )


def register_handlers(
    app: AsyncApp,
    events: EventFilter,
    sink: Callable[[Decision, Mapping[str, Any]], Awaitable[None]] | None = None,
) -> None:
    """Route message and reaction events through the filter to the log, then ``sink``.

    ``sink`` gets the raw event too, for the keyword handler to read its text.
    """

    async def on_event(event: dict[str, Any], body: dict[str, Any]) -> None:
        event_id = body.get("event_id")
        decision = events.classify(event, event_id if isinstance(event_id, str) else None)
        log_decision(decision)
        if sink is not None:
            await sink(decision, event)

    app.event("message")(on_event)
    app.event("reaction_added")(on_event)


def register_commands(
    app: AsyncApp,
    policy: Policy | None = None,
    mode_for: Callable[[str], Awaitable[ChannelMode]] | None = None,
) -> None:
    """Answer ``/jotflow`` privately. Only ``help`` exists so far.

    With ``policy``, help knows the channel it was asked in: its repos and how
    approval works there, or that the channel is not set up (#80).
    """

    async def on_command(ack: Any, command: dict[str, Any]) -> None:
        text = str(command.get("text") or "").strip().lower()
        if text in ("", "help"):
            channel = str(command.get("channel_id") or "")
            if policy is None:
                await ack(help_text())
                return
            mode = ChannelMode.PROPOSE
            if mode_for is not None and channel in policy.channels:
                mode = await mode_for(channel)
            await ack(channel_help(policy, channel, mode))
        else:
            await ack("Only /jotflow help exists so far. It lists the keywords.")

    app.command(SLASH_COMMAND)(on_command)


class SlackThreadPoster:
    """Posts the agent's clarifying questions as thread replies (``ask_channel``, #15).

    The text arrives cleaned of broadcast mentions and escaped. Links and media
    never unfurl, and names are never turned into mentions.
    """

    def __init__(self, client: AsyncWebClient) -> None:
        self._client = client

    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        from jotflow.whiteboard.write_tools import PostError

        try:
            response = await self._client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=text,
                link_names=False,
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackApiError as exc:
            raise PostError(str(exc.response.get("error", "unknown_error"))) from None
        except NETWORK_ERRORS as exc:
            raise PostError(f"unreachable: {type(exc).__name__}") from None
        ts = response.get("ts")
        if not isinstance(ts, str):
            raise PostError("no ts in Slack's reply")
        return ts
