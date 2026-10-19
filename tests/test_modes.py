"""Channel modes and /jotflow mode (#28)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse as SlackResponse

from jotflow.models import ChannelMode, ProposalState, RunKind
from jotflow.orchestrator.modes import (
    ALREADY,
    APPROVERS_ONLY,
    MAX_MEMBERS_CHECKED,
    MAX_USER_PAGES,
    MEANING,
    NO_GUESTS,
    NOT_SET_UP,
    USAGE,
    USERS_PAGE_SIZE,
    AutoApprover,
    BoltModeSlack,
    ModeCommand,
)
from jotflow.orchestrator.packet import SlackFetchError
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


async def test_the_slash_command_acks_first_then_answers_mode(store: Store) -> None:
    slack = FakeModeSlack()
    app = FakeApp()
    register_commands(cast(Any, app), policy(), None, ModeCommand(store, policy(), slack))
    events: list[str] = []

    async def ack(answer: str | None = None) -> None:
        events.append(f"ack:{answer}")

    async def respond(answer: str) -> None:
        events.append(f"respond:{answer}")

    command = {"text": "Mode Auto", "channel_id": CH, "user_id": ALEX}
    await app.commands[SLASH_COMMAND](ack=ack, command=command, respond=respond)
    assert events == ["ack:None", "respond:Done: JotFlow is in auto mode here."]
    assert len(slack.announced) == 1


async def test_a_failed_member_check_still_announces_with_the_warning(store: Store) -> None:
    class Failing(FakeModeSlack):
        async def outsiders(self, channel_id: str) -> str | None:
            raise RuntimeError("ratelimited")

    slack = Failing()
    assert (await ModeCommand(store, policy(), slack).handle(CH, ALEX, "auto")).changed
    ((_, text),) = slack.announced
    assert "Warning: this channel has members JotFlow could not check." in text


# --- the Bolt adapter ------------------------------------------------------------------------


OWN_TEAM = "T0OURS"


def member(user_id: str, **flags: Any) -> dict[str, Any]:
    return {"id": user_id, "team_id": OWN_TEAM, **flags}


class FakeClient:
    """conversations.members and users.list, paged, counting calls; can rate limit."""

    def __init__(
        self,
        channel: dict[str, Any],
        pages: list[list[str]],
        directory: list[dict[str, Any]] | None = None,
    ) -> None:
        self.channel = channel
        self.pages = pages
        if directory is None:
            directory = [member(m) for page in pages for m in page]
        self.directory = directory
        self.posts: list[dict[str, Any]] = []
        self.member_calls = 0
        self.list_calls = 0
        self.info_calls = 0
        self.limited: list[str] = []  # methods to answer 429 once each, in order
        self.fail_list = False

    def _maybe_limit(self, method: str) -> None:
        if self.limited and self.limited[0] == method:
            self.limited.pop(0)
            raise SlackApiError(
                "ratelimited",
                SlackResponse(
                    client=None,
                    http_verb="POST",
                    api_url=method,
                    req_args={},
                    data={"ok": False, "error": "ratelimited"},
                    headers={"Retry-After": "1"},
                    status_code=429,
                ),
            )

    async def conversations_info(self, *, channel: str) -> dict[str, Any]:
        self._maybe_limit("conversations.info")
        return {"ok": True, "channel": self.channel}

    async def conversations_members(
        self, *, channel: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        self._maybe_limit("conversations.members")
        index = int(cursor or 0)
        self.member_calls += 1
        more = index + 1 < len(self.pages)
        return {
            "ok": True,
            "members": self.pages[index],
            "response_metadata": {"next_cursor": str(index + 1) if more else ""},
        }

    async def users_list(self, *, limit: int, cursor: str | None) -> dict[str, Any]:
        self._maybe_limit("users.list")
        if self.fail_list:
            raise SlackApiError(
                "missing_scope",
                SlackResponse(
                    client=None,
                    http_verb="POST",
                    api_url="users.list",
                    req_args={},
                    data={"ok": False, "error": "missing_scope"},
                    headers={},
                    status_code=200,
                ),
            )
        self.list_calls += 1
        start = int(cursor or 0)
        end = start + limit
        more = end < len(self.directory)
        return {
            "ok": True,
            "members": self.directory[start:end],
            "response_metadata": {"next_cursor": str(end) if more else ""},
        }

    async def users_info(self, *, user: str) -> dict[str, Any]:  # pragma: no cover
        self.info_calls += 1
        raise AssertionError("the guest scan never calls users.info")

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1.0"}


class FakePeople:
    def __init__(self, guests: set[str] = frozenset(), own: str | None = OWN_TEAM) -> None:  # type: ignore[assignment]
        self.guests = guests
        self.own = own

    async def person(self, user_id: str) -> Person:
        return Person(user_id, guest=user_id in self.guests)

    async def own_team(self) -> str | None:
        return self.own


class Names:
    async def name(self, user_id: str) -> str | None:
        return None


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def adapter(client: FakeClient, people: FakePeople | None = None) -> BoltModeSlack:
    return BoltModeSlack(client, people or FakePeople(), Names(), sleep=Sleeps())


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
    directory = [member(m, is_restricted=m in guests) for page in pages for m in page]
    slack = adapter(FakeClient(channel, pages, directory))
    assert await slack.outsiders(CH) == who


@pytest.mark.parametrize(
    "flags",
    [
        {"is_restricted": True},
        {"is_ultra_restricted": True},
        {"is_stranger": True},
        {"team_id": "T0THEIRS"},
    ],
)
async def test_each_kind_of_outsider_is_found_in_the_directory(flags: dict[str, Any]) -> None:
    client = FakeClient({}, [["U1", "U2"]], [member("U1"), member("U2", **flags)])
    assert await adapter(client).outsiders(CH) == "a guest"


async def test_a_member_the_directory_does_not_list_counts_as_a_guest() -> None:
    client = FakeClient({}, [["U1", "U2"]], [member("U1"), member("U9")])
    assert await adapter(client).outsiders(CH) == "a guest"


async def test_without_our_own_team_everyone_with_a_team_is_external() -> None:
    client = FakeClient({}, [["U1"]])
    assert await adapter(client, FakePeople(own=None)).outsiders(CH) == "a guest"


async def test_a_thousand_members_take_a_handful_of_calls() -> None:
    members = [f"U{n:05d}" for n in range(MAX_MEMBERS_CHECKED)]
    pages = [members[i : i + 200] for i in range(0, len(members), 200)]
    others = [member(f"W{n:05d}") for n in range(3000)]
    client = FakeClient({}, pages, others + [member(m) for m in members])
    assert await adapter(client).outsiders(CH) is None
    assert client.member_calls == 5
    assert client.list_calls == 8  # 4000 people, 500 a page
    assert client.info_calls == 0


async def test_the_directory_scan_stops_once_every_member_is_found() -> None:
    directory = [member("U1"), member("U2")] + [member(f"W{n}") for n in range(5000)]
    client = FakeClient({}, [["U1", "U2"]], directory)
    assert await adapter(client).outsiders(CH) is None
    assert client.list_calls == 1


async def test_a_directory_too_large_for_one_scan_warns() -> None:
    directory = [member(f"W{n}") for n in range(MAX_USER_PAGES * USERS_PAGE_SIZE)]
    client = FakeClient({}, [["U1"]], [*directory, member("U1")])
    assert await adapter(client).outsiders(CH) == "more members than JotFlow could check"
    assert client.list_calls == MAX_USER_PAGES


async def test_too_many_members_warns_anyway() -> None:
    members = [f"U{n:05d}" for n in range(MAX_MEMBERS_CHECKED + 5)]
    pages = [members[i : i + 200] for i in range(0, len(members), 200)]
    client = FakeClient({}, pages)
    assert await adapter(client).outsiders(CH) == "more members than JotFlow could check"


async def test_rate_limits_are_waited_out() -> None:
    client = FakeClient({}, [["U1"]], [member("U1")])
    client.limited = ["conversations.info", "conversations.members", "users.list"]
    sleeps = Sleeps()
    slack = BoltModeSlack(client, FakePeople(), Names(), sleep=sleeps)
    assert await slack.outsiders(CH) is None
    assert sleeps.waits == [1.0, 1.0, 1.0]


async def test_a_listing_rate_limited_past_its_budget_raises() -> None:
    client = FakeClient({}, [["U1"]], [member("U1")])
    client.limited = ["users.list"] * 50
    with pytest.raises(SlackFetchError, match="rate limiting the workspace's people"):
        await adapter(client).outsiders(CH)


async def test_a_failed_listing_raises() -> None:
    client = FakeClient({}, [["U1"]])
    client.fail_list = True
    with pytest.raises(SlackFetchError, match="missing_scope"):
        await adapter(client).outsiders(CH)


async def test_a_failed_listing_still_warns_through_the_command(store: Store) -> None:
    client = FakeClient({}, [["U1"]])
    client.fail_list = True
    slack = adapter(client)
    assert (await ModeCommand(store, policy(), slack).handle(CH, ALEX, "auto")).changed
    (post,) = client.posts
    assert "Warning: this channel has members JotFlow could not check." in post["text"]


async def test_the_adapter_announces_plain_text_without_unfurling() -> None:
    client = FakeClient({}, [[]])
    slack = BoltModeSlack(client, FakePeople(), Names())
    await slack.announce(CH, "Alex set JotFlow to auto mode in this channel.")
    (post,) = client.posts
    assert post["blocks"][0]["text"]["type"] == "plain_text"
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert await slack.name(ALEX) is None
    assert (await slack.person(ALEX)).guest is False
