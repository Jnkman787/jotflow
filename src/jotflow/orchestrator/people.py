"""Who a Slack user is, for decisions only a workspace member may make (#56, #27).

Guests and external Slack Connect users may not skip someone else's message
(D10) and may never approve (D7). Both decisions use this one check.

It fails closed: if JotFlow cannot learn its own workspace from ``auth.test``,
every user from any team counts as external.

The same ``auth.test`` gives the workspace URL that issue-body permalinks are
built from (#31), and :meth:`SlackPeople.authors` finds who wrote a message,
for approval (#151) and for an issue's "Raised in Slack by" line (#31).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jotflow.models import MessageRef

_WORKSPACE_URL = re.compile(r"https://[a-z0-9-]+\.slack\.com/")
# A user mention in a Slack message's text: <@U0123ABCD> or <@U0123ABCD|name>.
_MENTION = re.compile(r"<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>")


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
        self._url: str | None = None
        self._asked = False

    async def _auth(self) -> None:
        if self._asked:
            return
        response = await self._client.auth_test()
        self._asked = True
        team = response.get("team_id")
        if self._team_id is None:
            self._team_id = team if isinstance(team, str) else None
        url = response.get("url")
        self._url = url if isinstance(url, str) and _WORKSPACE_URL.fullmatch(url) else None

    async def own_team(self) -> str | None:
        """This workspace's id, from ``auth.test`` once, unless it was given."""
        if self._team_id is None:
            await self._auth()
        return self._team_id

    async def workspace_url(self) -> str | None:
        """``https://<name>.slack.com/``, from the same ``auth.test``; None if unusable."""
        await self._auth()
        return self._url

    async def authors(self, refs: Sequence[MessageRef]) -> set[str]:
        """The people who wrote these messages, from Slack. A bot's post adds no one.

        Raises :class:`LookupError` if a message cannot be found, so its author is
        never assumed.
        """
        found: set[str] = set()
        for ref in refs:
            parent = ref.thread_ts or ref.ts
            response = await self._client.conversations_replies(
                channel=ref.channel_id,
                ts=parent,
                oldest=ref.ts,
                latest=ref.ts,
                inclusive=True,
                limit=1,
            )
            messages = response.get("messages") or []
            match = next(
                (m for m in messages if isinstance(m, Mapping) and m.get("ts") == ref.ts), None
            )
            if match is None:
                # Deleted, or never there: who wrote it cannot be confirmed.
                raise LookupError(f"message {ref.ts} was not found")
            user = match.get("user")
            if isinstance(user, str):
                found.add(user)
            elif match.get("bot_id") or match.get("subtype") == "bot_message":
                continue  # an integration's post has no person behind it
            else:
                raise LookupError(f"no author for message {ref.ts}")
        return found

    async def mentions(self, ref: MessageRef) -> set[str]:
        """The people a message @-mentions, from Slack (#305). Raises :class:`LookupError`
        if the message can't be found, so a mention is never assumed."""
        parent = ref.thread_ts or ref.ts
        response = await self._client.conversations_replies(
            channel=ref.channel_id, ts=parent, oldest=ref.ts, latest=ref.ts, inclusive=True, limit=1
        )
        messages = response.get("messages") or []
        match = next(
            (m for m in messages if isinstance(m, Mapping) and m.get("ts") == ref.ts), None
        )
        if match is None:
            raise LookupError(f"message {ref.ts} was not found")
        text = match.get("text")
        return set(_MENTION.findall(text)) if isinstance(text, str) else set()

    async def person(self, user_id: str) -> Person:
        response = await self._client.users_info(user=user_id)
        return classify(user_id, response.get("user") or {}, await self.own_team())


def classify(user_id: str, user: Mapping[str, Any], own_team: str | None) -> Person:
    """A Slack user object, from ``users.info`` or ``users.list``, as a :class:`Person`.

    With no known ``own_team``, anyone with a team counts as external.
    """
    guest = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
    team = user.get("team_id")
    external = bool(user.get("is_stranger")) or (team is not None and team != own_team)
    return Person(user_id, guest=guest, external=external)


class SlackAttribution:
    """What an execution run's issue footer needs from Slack (#31): who wrote a
    message, people's names, and the workspace URL, sharing one ``auth.test``."""

    def __init__(self, people: SlackPeople, name: Callable[[str], Awaitable[str | None]]) -> None:
        self._people = people
        self._name = name

    async def authors(self, refs: Sequence[MessageRef]) -> set[str]:
        return await self._people.authors(refs)

    async def mentions(self, ref: MessageRef) -> set[str]:
        return await self._people.mentions(ref)

    async def name(self, user_id: str) -> str | None:
        return await self._name(user_id)

    async def workspace_url(self) -> str | None:
        return await self._people.workspace_url()
