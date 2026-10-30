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
# A ledger row get_work writes, never a tool: each change request's card, by ts (#375).
PACKET_CHANGE_REQUESTS = "packet_change_requests"
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,20}")
_PROPOSAL_ID = re.compile(r"[0-9a-f]{16}")


def change_request_metadata(
    user_id: str, proposal_id: str | None = None, topic_id: str | None = None
) -> dict[str, Any]:
    """The metadata JotFlow's post of ``user_id``'s change request carries: who asked,
    and which card it's for, so with two cards in a thread the agent revises the
    right one (#375)."""
    payload: dict[str, Any] = {"by": user_id}
    if proposal_id is not None:
        payload["proposal"] = proposal_id
    if topic_id is not None:
        payload["topic"] = topic_id
    return {"event_type": CHANGE_REQUEST_EVENT, "event_payload": payload}


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


def change_request_for(message: Mapping[str, Any], bot_user_id: str | None) -> str | None:
    """The proposal a change request is for, or None: an older post names none (#375)."""
    if change_request_by(message, bot_user_id) is None:
        return None
    payload = message["metadata"].get("event_payload")
    proposal = payload.get("proposal") if isinstance(payload, Mapping) else None
    return proposal if isinstance(proposal, str) and _PROPOSAL_ID.fullmatch(proposal) else None
