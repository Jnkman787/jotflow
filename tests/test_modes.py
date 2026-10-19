"""Channel modes and /jotflow mode (#28)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from jotflow.models import ChannelMode, ProposalState, RunKind
from jotflow.orchestrator.modes import (
    ALREADY,
    APPROVERS_ONLY,
    MAX_MEMBERS_CHECKED,
    MEANING,
    NO_GUESTS,
    NOT_SET_UP,
    USAGE,
    AutoApprover,
    BoltModeSlack,
    ModeCommand,
)
from jotflow.orchestrator.people import Person
from jotflow.orchestrator.slack import register_commands
from jotflow.policy import Policy
from jotflow.slack_manifest import SLASH_COMMAND
from jotflow.store import MemoryStore, SqliteStore, Store
from tests.test_card_sync import proposal

CH = "C0IDEAS01"
ALEX, LEAD = "U0ALEX", "U0LEAD"
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}


def policy(approvers: tuple[str, ...] = ()) -> Policy:
    return Policy.model_validate(
        {"approval": {"approvers": list(approvers)}, "channels": {CH: {"repos": [WEB]}}}
    )


class FakeModeSlack:
    def __init__(self, people: dict[str, Person] | None = None, outsiders: str | None = None):
        self.people = people or {}
        self.announced: list[tuple[str, str]] = []
        self.who = outsiders

    async def person(self, user_id: str) -> Person:
        return self.people.get(user_id, Person(user_id))

    async def name(self, user_id: str) -> str | None:
        return {ALEX: "Alex", LEAD: "Lee"}.get(user_id)

    async def announce(self, channel_id: str, text: str) -> None:
        self.announced.append((channel_id, text))

    async def outsiders(self, channel_id: str) -> str | None:
        return self.who


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


# --- /jotflow mode -----------------------------------------------------------------


async def test_the_mode_starts_as_propose_and_says_what_that_means(store: Store) -> None:
    reply = await ModeCommand(store, policy(), FakeModeSlack()).handle(CH, ALEX, "")
    assert reply.text.startswith("JotFlow is in propose mode here.")
    assert MEANING[ChannelMode.PROPOSE] in reply.text and USAGE in reply.text
    assert not reply.changed


async def test_setting_a_mode_stores_it_and_announces_it_in_plain_words(store: Store) -> None:
    slack = FakeModeSlack()
    command = ModeCommand(store, policy(), slack)
    reply = await command.handle(CH, ALEX, "paused")
    assert reply.changed and "paused" in reply.text
    setting = await store.get_channel_mode(CH)
    assert setting is not None and setting.mode is ChannelMode.PAUSED
    assert setting.changed_by == ALEX
    ((channel, text),) = slack.announced
    assert channel == CH
    assert (
        text == "Alex set JotFlow to paused mode in this channel.\n" + MEANING[ChannelMode.PAUSED]
    )
    assert "<@" not in text and "<!" not in text
    shown = await command.handle(CH, LEAD, "")
    assert "paused mode" in shown.text and "Set by Alex" in shown.text


async def test_setting_the_same_mode_announces_nothing(store: Store) -> None:
    slack = FakeModeSlack()
    reply = await ModeCommand(store, policy(), slack).handle(CH, ALEX, "propose")
    assert reply.text == ALREADY.format(mode="propose") and not reply.changed
    assert slack.announced == []


@pytest.mark.parametrize("argument", ["fast", "auto please", "PAUSE"])
async def test_an_unknown_mode_shows_the_usage(store: Store, argument: str) -> None:
    reply = await ModeCommand(store, policy(), FakeModeSlack()).handle(CH, ALEX, argument)
    assert reply.text == USAGE
    assert await store.get_channel_mode(CH) is None


async def test_an_unconfigured_channel_has_no_mode(store: Store) -> None:
    reply = await ModeCommand(store, policy(), FakeModeSlack()).handle("C0ELSE01", ALEX, "auto")
    assert reply.text == NOT_SET_UP
    assert await store.get_channel_mode("C0ELSE01") is None


@pytest.mark.parametrize("person", [Person(ALEX, guest=True), Person(ALEX, external=True)])
async def test_guests_and_external_users_never_change_the_mode(
    store: Store, person: Person
) -> None:
    slack = FakeModeSlack({ALEX: person})
    assert (await ModeCommand(store, policy(), slack).handle(CH, ALEX, "auto")).text == NO_GUESTS
    assert await store.get_channel_mode(CH) is None and slack.announced == []


async def test_with_approvers_on_only_an_approver_changes_the_mode(store: Store) -> None:
    slack = FakeModeSlack()
    command = ModeCommand(store, policy(approvers=(LEAD,)), slack)
    assert (await command.handle(CH, ALEX, "auto")).text == APPROVERS_ONLY
    assert (await command.handle(CH, ALEX, "")).text.startswith("JotFlow is in propose mode")
    assert (await command.handle(CH, LEAD, "auto")).changed


async def test_auto_in_a_channel_with_outsiders_warns_in_the_announcement(store: Store) -> None:
    slack = FakeModeSlack(outsiders="a guest")
    await ModeCommand(store, policy(), slack).handle(CH, ALEX, "auto")
    ((_, text),) = slack.announced
    assert "Warning: this channel has a guest." in text
    assert "no human review" in text


async def test_auto_without_outsiders_has_no_warning(store: Store) -> None:
    slack = FakeModeSlack()
    await ModeCommand(store, policy(), slack).handle(CH, ALEX, "auto")
    assert "Warning" not in slack.announced[0][1]


async def test_only_auto_checks_for_outsiders(store: Store) -> None:
    slack = FakeModeSlack(outsiders="a guest")
    await ModeCommand(store, policy(), slack).handle(CH, ALEX, "paused")
    assert "Warning" not in slack.announced[0][1]


# --- auto mode approves new proposals ---------------------------------------------------


async def _mode(store: Store, channel_id: str) -> ChannelMode:
    setting = await store.get_channel_mode(channel_id)
    return ChannelMode.PROPOSE if setting is None else setting.mode


async def test_auto_approves_a_new_proposal_and_queues_its_write(store: Store) -> None:
    await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by=ALEX)
    await store.add_proposal(proposal())
    auto = AutoApprover(store, lambda c: _mode(store, c))
    assert await auto.approve_due() == 1
    record = await store.get_proposal("p1")
    assert record is not None and record.state is ProposalState.APPROVED
    assert record.approved_by == "auto" and record.content_hash is not None
    runs = await store.thread_runs(CH, record.thread_ts)
    assert [r.kind for r in runs] == [RunKind.EXECUTION]
    assert await auto.approve_due() == 0


async def test_propose_mode_leaves_proposals_for_a_person(store: Store) -> None:
    await store.add_proposal(proposal())
    assert await AutoApprover(store, lambda c: _mode(store, c)).approve_due() == 0
    record = await store.get_proposal("p1")
    assert record is not None and record.state is ProposalState.PENDING


async def test_switching_to_auto_never_approves_a_card_already_shown(store: Store) -> None:
    await store.add_proposal(proposal())
    await store.set_card_ts("p1", "1760782000.000001")
    await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by=ALEX)
    assert await AutoApprover(store, lambda c: _mode(store, c)).approve_due() == 0


async def test_a_proposal_that_changed_meanwhile_is_skipped(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow.store.base import TransitionError

    await store.set_channel_mode(CH, ChannelMode.AUTO, changed_by=ALEX)
    await store.add_proposal(proposal())

    async def lost(*_: Any, **__: Any) -> Any:
        raise TransitionError("p1", ProposalState.SUPERSEDED, ProposalState.APPROVED)

    monkeypatch.setattr(store, "transition", lost)
    assert await AutoApprover(store, lambda c: _mode(store, c)).approve_due() == 0
    assert await store.thread_runs(CH, "1760781600.000100") == []


# --- the slash command ---------------------------------------------------------------------


class FakeApp:
    def __init__(self) -> None:
        self.commands: dict[str, Callable[..., Any]] = {}

    def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.commands[name] = fn
            return fn

        return register


async def test_the_slash_command_routes_mode(store: Store) -> None:
    slack = FakeModeSlack()
    app = FakeApp()
    register_commands(cast(Any, app), policy(), None, ModeCommand(store, policy(), slack))
    answers: list[str] = []

    async def ack(answer: str) -> None:
        answers.append(answer)

    command = {"text": "Mode Auto", "channel_id": CH, "user_id": ALEX}
    await app.commands[SLASH_COMMAND](ack=ack, command=command)
    assert answers == ["Done: JotFlow is in auto mode here."]
    assert len(slack.announced) == 1


# --- the Bolt adapter ------------------------------------------------------------------------


class FakeClient:
    def __init__(self, channel: dict[str, Any], pages: list[list[str]]) -> None:
        self.channel = channel
        self.pages = pages
        self.posts: list[dict[str, Any]] = []
        self.member_calls = 0

    async def conversations_info(self, *, channel: str) -> dict[str, Any]:
        return {"ok": True, "channel": self.channel}

    async def conversations_members(
        self, *, channel: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        index = int(cursor or 0)
        self.member_calls += 1
        more = index + 1 < len(self.pages)
        return {
            "ok": True,
            "members": self.pages[index],
            "response_metadata": {"next_cursor": str(index + 1) if more else ""},
        }

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1.0"}


class FakePeople:
    def __init__(self, guests: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.guests = guests

    async def person(self, user_id: str) -> Person:
        return Person(user_id, guest=user_id in self.guests)


class Names:
    async def name(self, user_id: str) -> str | None:
        return None


@pytest.mark.parametrize(
    ("channel", "pages", "guests", "who"),
    [
        ({}, [["U1", "U2"]], set(), None),
        ({}, [["U1"], ["U2", "U3"]], {"U3"}, "a guest"),
        ({}, [["U1", "U2"]], {"U1", "U2"}, "guests"),
        ({"is_ext_shared": True}, [["U1"]], set(), "members from another organisation"),
        ({"is_shared": True}, [["U1"]], {"U1"}, "members from another organisation and a guest"),
    ],
)
async def test_the_adapter_finds_guests_and_shared_channels(
    channel: dict[str, Any], pages: list[list[str]], guests: set[str], who: str | None
) -> None:
    slack = BoltModeSlack(FakeClient(channel, pages), FakePeople(guests), Names())
    assert await slack.outsiders(CH) == who


async def test_too_many_members_warns_anyway() -> None:
    members = [f"U{n:05d}" for n in range(MAX_MEMBERS_CHECKED + 5)]
    pages = [members[i : i + 200] for i in range(0, len(members), 200)]
    client = FakeClient({}, pages)
    slack = BoltModeSlack(client, FakePeople(), Names())
    assert await slack.outsiders(CH) == "more members than JotFlow could check"


async def test_the_adapter_announces_plain_text_without_unfurling() -> None:
    client = FakeClient({}, [[]])
    slack = BoltModeSlack(client, FakePeople(), Names())
    await slack.announce(CH, "Alex set JotFlow to auto mode in this channel.")
    (post,) = client.posts
    assert post["blocks"][0]["text"]["type"] == "plain_text"
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert await slack.name(ALEX) is None
    assert (await slack.person(ALEX)).guest is False
