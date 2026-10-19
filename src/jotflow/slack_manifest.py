"""Render the Slack app manifest a team pastes into Slack to create its JotFlow app.

Each team creates a dedicated, internal Slack app from this manifest (see the
Permissions section of docs/architecture.md). The bot name is the team's choice;
"JotFlow" is only the default. Regenerate the checked-in default with
``make manifest``.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

DEFAULT_BOT_NAME = "JotFlow"

# Slack caps an app name at 35 characters. The same value names the bot user,
# so the stricter limit applies to both.
MAX_BOT_NAME_LENGTH = 35

_BOT_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]*")

# v1 bot scopes, from the Permissions section of docs/architecture.md.
# app_mentions:read arrives with questions in M5 (D12).
# No channels:join: a team member invites the bot to each channel with /invite.
BOT_SCOPES: tuple[str, ...] = (
    "channels:history",  # read messages and threads in public channels the bot is in
    "groups:history",  # read messages and threads in private channels the bot is in
    "channels:read",  # check membership and detect shared public channels (D7 auto warning)
    "groups:read",  # the same for private channels
    "chat:write",  # post proposal cards, questions, and issue links
    "reactions:read",  # keyword reactions and optional reaction approval (D7, D10)
    "reactions:write",  # acknowledge a recognised keyword with a reaction
    "pins:write",  # pin the short help message when JotFlow joins a channel
    "users:read",  # display names on cards; skip guests and external users
    "commands",  # the /jotflow slash command
)

# message.channels and message.groups need the two history scopes;
# reaction_added (D7, D10) needs reactions:read.
BOT_EVENTS: tuple[str, ...] = (
    "message.channels",
    "message.groups",
    "reaction_added",
    "reaction_removed",  # taking a keyword's reaction back (#138)
)

SLASH_COMMAND = "/jotflow"


class BotNameError(ValueError):
    """The requested bot name cannot be used in a Slack app manifest."""


def validate_bot_name(name: str) -> str:
    """Return the bot name with surrounding whitespace removed, or raise BotNameError."""
    cleaned = name.strip()
    if not cleaned:
        raise BotNameError("Bot name must not be empty.")
    if len(cleaned) > MAX_BOT_NAME_LENGTH:
        raise BotNameError(
            f"Bot name must be at most {MAX_BOT_NAME_LENGTH} characters; got {len(cleaned)}."
        )
    if not _BOT_NAME_PATTERN.fullmatch(cleaned):
        raise BotNameError(
            "Bot name may contain only letters, digits, spaces, '.', '-' and '_', "
            "and must start with a letter or digit."
        )
    return cleaned


def render_manifest(bot_name: str = DEFAULT_BOT_NAME) -> dict[str, Any]:
    """Return the Slack app manifest for a JotFlow app with the given bot name."""
    name = validate_bot_name(bot_name)
    return {
        "_metadata": {"major_version": 1, "minor_version": 1},
        "display_information": {
            "name": name,
            "description": "Turns ideas and bugs in this channel into GitHub issues.",
        },
        "features": {
            "bot_user": {"display_name": name, "always_online": True},
            # No DM or Home tab: the app has no im scopes, so DMs would vanish unread.
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": False,
                "messages_tab_read_only_enabled": False,
            },
            "slash_commands": [
                {
                    "command": SLASH_COMMAND,
                    "description": f"Help, channel mode, and account linking for {name}",
                    "usage_hint": "help",
                    "should_escape": False,
                }
            ],
        },
        "oauth_config": {"scopes": {"bot": list(BOT_SCOPES)}},
        "settings": {
            "event_subscriptions": {"bot_events": list(BOT_EVENTS)},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def render_manifest_json(bot_name: str = DEFAULT_BOT_NAME) -> str:
    """Return the manifest as stable, indented JSON ending in a newline."""
    return json.dumps(render_manifest(bot_name), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Print the manifest for the bot name given as the only argument, or the default."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: python -m jotflow.slack_manifest [BOT_NAME]", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(render_manifest_json(args[0] if args else DEFAULT_BOT_NAME))
    except BotNameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
