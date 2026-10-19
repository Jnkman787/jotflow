"""Who a Slack user is, for decisions only a workspace member may make (#56, #27).

Guests and external Slack Connect users may not skip someone else's message
(D10) and may never approve (D7). Both decisions use this one check.

It fails closed: if JotFlow cannot learn its own workspace from ``auth.test``,
every user from any team counts as external.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Person:
    """What JotFlow needs to know about who acted."""

    user_id: str
    guest: bool = False
    external: bool = False


class SlackPeople:
    """Looks people up with ``users.info``, against this workspace's own team."""

    def __init__(self, client: Any, team_id: str | None = None) -> None:
        self._client = client
        self._team_id = team_id

    async def own_team(self) -> str | None:
        """This workspace's id, from ``auth.test`` once, unless it was given."""
        if self._team_id is None:
            response = await self._client.auth_test()
            team = response.get("team_id")
            self._team_id = team if isinstance(team, str) else None
        return self._team_id

    async def person(self, user_id: str) -> Person:
        response = await self._client.users_info(user=user_id)
        user = response.get("user") or {}
        guest = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
        team = user.get("team_id")
        own = await self.own_team()
        external = bool(user.get("is_stranger")) or (team is not None and team != own)
        return Person(user_id, guest=guest, external=external)
