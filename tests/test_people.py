"""Who people are, and where the workspace is, from one auth.test (#56, #27, #31)."""

from __future__ import annotations

from typing import Any

import pytest

from jotflow.models import MessageRef
from jotflow.orchestrator.people import SlackPeople


class Client:
    def __init__(self, auth: dict[str, Any], users: dict[str, Any] | None = None) -> None:
        self.auth = auth
        self.users = users or {}
        self.calls: list[str] = []
        self.messages: dict[str, dict[str, Any]] = {}

    async def auth_test(self) -> dict[str, Any]:
        self.calls.append("auth.test")
        return self.auth

    async def users_info(self, user: str) -> dict[str, Any]:
        self.calls.append(f"users.info {user}")
        return {"user": self.users.get(user, {})}

    async def conversations_replies(self, **kw: Any) -> dict[str, Any]:
        found = self.messages.get(kw["latest"])
        return {"messages": [found] if found else []}


async def test_one_auth_test_gives_the_team_and_the_workspace_url() -> None:
    client = Client({"team_id": "T0ACME", "url": "https://acme-co.slack.com/"})
    people = SlackPeople(client)
    assert await people.workspace_url() == "https://acme-co.slack.com/"
    assert await people.own_team() == "T0ACME"
    assert await people.workspace_url() == "https://acme-co.slack.com/"
    assert client.calls == ["auth.test"]


@pytest.mark.parametrize(
    "url", [None, "", "http://acme.slack.com/", "https://evil.example/", "https://a.slack.com/x"]
)
async def test_an_unusable_workspace_url_is_none(url: str | None) -> None:
    people = SlackPeople(Client({"team_id": "T0ACME", "url": url}), team_id="T0GIVEN")
    assert await people.workspace_url() is None
    assert await people.own_team() == "T0GIVEN"  # a given team is kept


async def test_authors_skip_bots_and_refuse_to_guess() -> None:
    client = Client({})
    client.messages = {
        "1.0": {"ts": "1.0", "user": "U1"},
        "2.0": {"ts": "2.0", "bot_id": "B1"},
        "3.0": {"ts": "3.0"},
    }
    people = SlackPeople(client)
    ref = MessageRef(channel_id="C0EXAMPLE1", ts="1.0")
    assert await people.authors([ref, MessageRef(channel_id="C0EXAMPLE1", ts="2.0")]) == {"U1"}
    with pytest.raises(LookupError, match="no author"):
        await people.authors([MessageRef(channel_id="C0EXAMPLE1", ts="3.0")])
    with pytest.raises(LookupError, match="not found"):
        await people.authors([MessageRef(channel_id="C0EXAMPLE1", ts="4.0")])
