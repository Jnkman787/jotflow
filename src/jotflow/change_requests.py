"""Who a change request is from, carried with JotFlow's own post of it (#330).

Request Changes posts the person's note in the thread as JotFlow's message, so
Slack names the bot as its author. The post carries message metadata naming the
person. Only JotFlow's code sets metadata on a post; an agent's question never
carries any. So a message counts as a person's change request only when it is
JotFlow's own and has this metadata, and then that person is its author: for the
drafting packet, for who revised a draft, and for who an issue credits.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

CHANGE_REQUEST_EVENT = "jotflow_change_request"
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")


def change_request_metadata(user_id: str) -> dict[str, Any]:
    """The metadata JotFlow's post of ``user_id``'s change request carries."""
    return {"event_type": CHANGE_REQUEST_EVENT, "event_payload": {"by": user_id}}


def change_request_by(message: Mapping[str, Any], bot_user_id: str | None) -> str | None:
    """The person a Slack message is a change request from, or None.

    Only JotFlow's own message counts, by its bot user id: another app could post
    the same metadata.
    """
    if bot_user_id is None or message.get("user") != bot_user_id:
        return None
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("event_type") != CHANGE_REQUEST_EVENT:
        return None
    payload = metadata.get("event_payload")
    by = payload.get("by") if isinstance(payload, Mapping) else None
    return by if isinstance(by, str) and _SLACK_USER.fullmatch(by) else None
