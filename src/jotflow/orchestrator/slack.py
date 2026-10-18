"""The Slack side of ingest: identity, channel membership, and event listeners."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from jotflow.orchestrator.ingest import BotIdentity, EventFilter, log_decision

log = logging.getLogger("jotflow.slack")


class SlackSetupError(Exception):
    """Slack rejected JotFlow's credentials or scopes at startup."""


async def fetch_identity(client: AsyncWebClient) -> tuple[BotIdentity, str]:
    """The bot's ids, and its display name for the ``/invite`` hint, from ``auth.test``."""
    try:
        response = await client.auth_test()
    except SlackApiError as exc:
        raise SlackSetupError(
            f"Slack rejected the bot token ({exc.response.get('error', 'unknown error')}); "
            "check JOTFLOW_SLACK_BOT_TOKEN"
        ) from None
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


def register_handlers(app: AsyncApp, events: EventFilter) -> None:
    """Route message and reaction events through the filter to the log."""

    async def on_event(event: dict[str, Any], body: dict[str, Any]) -> None:
        event_id = body.get("event_id")
        log_decision(events.classify(event, event_id if isinstance(event_id, str) else None))

    app.event("message")(on_event)
    app.event("reaction_added")(on_event)
