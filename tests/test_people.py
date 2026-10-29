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


# A change request JotFlow posted for a person is theirs (#330).


async def test_a_change_request_is_its_requesters_and_jotflows_own_post_no_ones() -> None:
    from jotflow.change_requests import change_request_metadata
    from jotflow.models import MessageRef

    client = Client({"team_id": "T0ACME", "user_id": "U0JOTBOT"})
    client.messages = {
        "1.1": {"ts": "1.1", "user": "U0JOTBOT", "metadata": change_request_metadata("U0ALICE")},
        "1.2": {"ts": "1.2", "user": "U0JOTBOT", "bot_id": "B0JOTBOT", "text": "Which repo?"},
        # Another app's post with the same metadata names nobody.
        "1.3": {
            "ts": "1.3",
            "user": "U0OTHERAPP",
            "bot_id": "B0OTHER",
            "metadata": change_request_metadata("U0ALICE"),
        },
        "1.4": {"ts": "1.4", "user": "U0BOB"},
    }
    people = SlackPeople(client)
    ref = lambda ts: MessageRef(channel_id="C0EXAMPLE1", ts=ts, thread_ts="1.0")  # noqa: E731
    assert await people.authors([ref("1.1")]) == {"U0ALICE"}
    assert await people.authors([ref("1.2")]) == set()
    assert await people.authors([ref("1.3")]) == {"U0OTHERAPP"}
    assert await people.authors([ref("1.4"), ref("1.1")]) == {"U0BOB", "U0ALICE"}
    assert client.calls.count("auth.test") == 1


def test_only_a_well_formed_change_request_names_someone() -> None:
    from jotflow.change_requests import change_request_by, change_request_metadata

    own = {"user": "U0JOTBOT", "metadata": change_request_metadata("U0ALICE")}
    assert change_request_by(own, "U0JOTBOT") == "U0ALICE"
    assert change_request_by(own, None) is None
    for metadata in (
        None,
        "text",
        {"event_type": "other", "event_payload": {"by": "U0ALICE"}},
        {"event_type": "jotflow_change_request", "event_payload": "U0ALICE"},
        {"event_type": "jotflow_change_request", "event_payload": {"by": "alice"}},
        {"event_type": "jotflow_change_request"},
    ):
        assert change_request_by({"user": "U0JOTBOT", "metadata": metadata}, "U0JOTBOT") is None
