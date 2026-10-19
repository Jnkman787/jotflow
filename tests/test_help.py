"""Help for people in the channel: one text source, and quiet one-time notices (#80)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from jotflow.models import ChannelMode
from jotflow.orchestrator.help import (
    NOT_SET_UP,
    TIP,
    BoltHelpSlack,
    HelpHandler,
    channel_help,
    guide_text,
    help_text,
    nudge,
)
from jotflow.orchestrator.keywords import KEYWORDS, REACTIONS
from jotflow.orchestrator.slack import register_commands
from jotflow.policy import Policy
from jotflow.slack_manifest import SLASH_COMMAND
from jotflow.store import MemoryStore, SqliteStore, Store

CH = "C0IDEAS01"
QUIET = "C0QUIET01"
ROOT = "1760781600.000100"
README = Path(__file__).resolve().parents[1] / "README.md"
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}
API = {"repo": "acme-co/api", "nickname": "api", "description": "API"}


def policy(**top: Any) -> Policy:
    return Policy.model_validate(
        {
            **top,
            "channels": {
                CH: {"repos": [WEB, API]},
                QUIET: {
                    "repos": [WEB],
                    "help": {"guide": False, "newcomer_tip": False, "near_miss_nudge": False},
                },
            },
        }
    )


class FakeHelpSlack:
    def __init__(self) -> None:
        self.whispers: list[tuple[str, str, str | None, str]] = []
        self.guides: list[tuple[str, str]] = []
        self.pins: list[tuple[str, str]] = []

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        self.whispers.append((channel_id, user_id, thread_ts, text))

    async def post_guide(self, channel_id: str, text: str) -> str:
        self.guides.append((channel_id, text))
        return "1760781900.000100"

    async def pin(self, channel_id: str, ts: str) -> None:
        self.pins.append((channel_id, ts))


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


def msg(text: str, user: str = "U0ALEX", ts: str = ROOT, **extra: Any) -> dict[str, Any]:
    return {"type": "message", "channel": CH, "ts": ts, "user": user, "text": text, **extra}


# --- one source of help text ------------------------------------------------------


def test_help_lists_every_keyword_and_reaction() -> None:
    text = help_text()
    for keyword in KEYWORDS:
        assert re.search(rf"^  {keyword}:", text, re.MULTILINE), keyword
    for reaction, keyword in REACTIONS.items():
        assert f":{reaction}: for {keyword}:" in text


def test_the_guide_names_every_keyword_and_the_help_command() -> None:
    guide = guide_text()
    for keyword in KEYWORDS:
        assert f"{keyword}:" in guide
    assert "/jotflow help" in guide and "after a person approves" in guide


def test_the_readme_shows_exactly_the_help_text() -> None:
    readme = README.read_text(encoding="utf-8")
    block = readme.split("<!-- jotflow-help:start -->\n", 1)[1].split("<!-- jotflow-help:end -->")[
        0
    ]
    assert block == f"```\n{help_text()}\n```\n"


@pytest.mark.parametrize("text", [help_text(), guide_text(), TIP, *(nudge(k) for k in KEYWORDS)])
def test_no_help_text_can_ping_or_link(text: str) -> None:
    assert "<" not in text and "@" not in text


def test_a_nudge_says_the_right_form_and_that_nothing_happened() -> None:
    text = nudge("skip")
    assert text.startswith("Did you mean skip:?")
    assert "JotFlow did not act on this message" in text


# --- /jotflow help in a channel ----------------------------------------------------


def test_help_in_a_channel_names_its_repos_and_approval() -> None:
    text = channel_help(policy(), CH, ChannelMode.PROPOSE)
    assert text.startswith(help_text())
    assert "This channel feeds: web (acme-co/web), api (acme-co/api)." in text
    assert "waits for a person to approve" in text
    assert "Anyone in the channel can approve." in text


def test_help_with_approvers_says_only_they_approve() -> None:
    text = channel_help(policy(approval={"approvers": ["U0LEAD"]}), CH, ChannelMode.PROPOSE)
    assert "Only the channel's approvers can approve." in text
    assert "U0LEAD" not in text


@pytest.mark.parametrize(
    ("mode", "line"),
    [(ChannelMode.AUTO, "writes each issue straight away"), (ChannelMode.PAUSED, "is paused here")],
)
def test_help_says_how_the_channels_mode_works(mode: ChannelMode, line: str) -> None:
    text = channel_help(policy(), CH, mode)
    assert line in text and "can approve" not in text


def test_help_in_an_unconfigured_channel_says_it_is_not_set_up() -> None:
    assert channel_help(policy(), "C0ELSEWHERE", ChannelMode.PROPOSE) == NOT_SET_UP


class FakeApp:
    def __init__(self) -> None:
        self.commands: dict[str, Callable[..., Any]] = {}

    def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.commands[name] = fn
            return fn

        return register


async def _ask(app: FakeApp, channel: str) -> str:
    answers: list[str] = []

    async def ack(answer: str) -> None:
        answers.append(answer)

    await app.commands[SLASH_COMMAND](ack=ack, command={"text": "help", "channel_id": channel})
    return answers[0]


async def test_the_slash_command_answers_for_its_channel() -> None:
    asked: list[str] = []

    async def mode_for(channel_id: str) -> ChannelMode:
        asked.append(channel_id)
        return ChannelMode.AUTO

    app = FakeApp()
    register_commands(cast(Any, app), policy(), mode_for)
    assert "writes each issue straight away" in await _ask(app, CH)
    assert await _ask(app, "C0ELSEWHERE") == NOT_SET_UP
    assert asked == [CH]


async def test_the_slash_command_without_a_mode_lookup_assumes_propose() -> None:
    app = FakeApp()
    register_commands(cast(Any, app), policy())
    assert "waits for a person to approve" in await _ask(app, CH)


# --- the handler: once only, and quiet ---------------------------------------------


async def test_the_guide_is_pinned_once_even_across_handlers(store: Store) -> None:
    slack = FakeHelpSlack()
    await HelpHandler(store, policy(), slack).ensure_guide(CH)
    await HelpHandler(store, policy(), slack).ensure_guide(CH)  # as after a restart
    assert slack.guides == [(CH, guide_text())]


async def test_no_guide_where_it_is_off_or_the_channel_is_unknown(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    await handler.ensure_guide(QUIET)
    await handler.ensure_guide("C0ELSEWHERE")
    assert slack.guides == []


async def test_a_first_message_gets_the_guide_and_one_tip(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    assert await handler.on_message(msg("Dark mode would be nice")) == "tip"
    assert await handler.on_message(msg("And a bigger font", ts="1760781700.000100")) is None
    assert slack.guides == [(CH, guide_text())]
    assert slack.whispers == [(CH, "U0ALEX", None, TIP)]


async def test_a_tip_in_a_thread_is_whispered_in_that_thread(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    await handler.on_message(msg("+1", ts="1760781700.000100", thread_ts=ROOT))
    assert slack.whispers[0][2] == ROOT


async def test_someone_who_uses_a_keyword_never_gets_the_tip(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    assert await handler.on_message(msg("idea: dark mode")) is None
    assert await handler.on_message(msg("and a font", ts="1760781700.000100")) is None
    assert slack.whispers == []


async def test_a_near_miss_gets_one_nudge_and_no_tip(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    assert await handler.on_message(msg("skip")) == "nudge"
    assert await handler.on_message(msg("Idea - dark mode", ts="1760781700.000100")) is None
    assert await handler.on_message(msg("plain text", ts="1760781800.000100")) is None
    assert slack.whispers == [(CH, "U0ALEX", None, nudge("skip"))]


async def test_each_person_gets_their_own(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    await handler.on_message(msg("hello"))
    await handler.on_message(msg("hello", user="U0BLAKE", ts="1760781700.000100"))
    assert [w[1] for w in slack.whispers] == ["U0ALEX", "U0BLAKE"]


async def test_nudges_off_falls_back_to_the_tip(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(help={"near_miss_nudge": False}), slack)
    assert await handler.on_message(msg("skip")) == "tip"


async def test_a_quiet_channel_gets_nothing(store: Store) -> None:
    slack = FakeHelpSlack()
    handler = HelpHandler(store, policy(), slack)
    assert await handler.on_message({**msg("skip"), "channel": QUIET}) is None
    assert await handler.on_message({**msg("hello", ts="2.0"), "channel": QUIET}) is None
    assert slack.whispers == slack.guides == []


@pytest.mark.parametrize(
    "event",
    [
        {"type": "message", "channel": CH, "ts": ROOT, "text": "no user"},
        {"type": "message", "channel": "C0ELSEWHERE", "ts": ROOT, "user": "U0ALEX", "text": "x"},
    ],
)
async def test_messages_without_a_person_or_a_channel_are_left_alone(
    store: Store, event: dict[str, Any]
) -> None:
    slack = FakeHelpSlack()
    assert await HelpHandler(store, policy(), slack).on_message(event) is None
    assert slack.whispers == slack.guides == []


# --- the Bolt adapter ----------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_postEphemeral", kwargs))
        return {"ok": True}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ok": True, "ts": "1760781900.000100"}

    async def pins_add(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("pins_add", kwargs))
        return {"ok": True}


def _plain(text: str) -> list[dict[str, Any]]:
    return [{"type": "section", "text": {"type": "plain_text", "text": text, "emoji": False}}]


async def test_the_adapter_whispers_plain_text_only_to_that_person() -> None:
    client = FakeClient()
    slack = BoltHelpSlack(client)
    await slack.whisper(CH, "U0ALEX", None, TIP)
    await slack.whisper(CH, "U0ALEX", ROOT, TIP)
    first, second = (kwargs for _, kwargs in client.calls)
    assert first == {"channel": CH, "user": "U0ALEX", "text": TIP, "blocks": _plain(TIP)}
    assert second["thread_ts"] == ROOT


async def test_the_adapter_posts_and_pins_the_guide_without_unfurling() -> None:
    client = FakeClient()
    slack = BoltHelpSlack(client)
    assert await slack.post_guide(CH, guide_text()) == "1760781900.000100"
    await slack.pin(CH, "1760781900.000100")
    (post_name, post), (pin_name, pin) = client.calls
    assert post_name == "chat_postMessage" and pin_name == "pins_add"
    assert post["blocks"] == _plain(guide_text())
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert pin == {"channel": CH, "timestamp": "1760781900.000100"}


async def test_a_failed_guide_post_is_retried_with_a_later_message(store: Store) -> None:
    slack = FakeHelpSlack()
    real_post = slack.post_guide
    failures = [RuntimeError("ratelimited")]

    async def flaky(channel_id: str, text: str) -> str:
        if failures:
            raise failures.pop()
        return await real_post(channel_id, text)

    slack.post_guide = flaky  # type: ignore[method-assign]
    handler = HelpHandler(store, policy(), slack)
    with pytest.raises(RuntimeError):
        await handler.on_message(msg("first"))
    assert slack.guides == []
    await handler.on_message(msg("second", ts="1760781700.000100"))
    assert slack.guides == [(CH, guide_text())]
    assert slack.pins == [(CH, "1760781900.000100")]
    await handler.on_message(msg("third", ts="1760781800.000100"))
    assert len(slack.guides) == 1


async def test_a_failed_pin_is_not_retried(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    slack = FakeHelpSlack()

    async def broken(channel_id: str, ts: str) -> None:
        raise RuntimeError("not_pinnable")

    slack.pin = broken  # type: ignore[method-assign]
    handler = HelpHandler(store, policy(), slack)
    with caplog.at_level("WARNING", logger="jotflow.help"):
        await handler.ensure_guide(CH)
    await HelpHandler(store, policy(), slack).ensure_guide(CH)
    assert len(slack.guides) == 1
    assert "could not pin it" in caplog.text


# --- /jotflow repos, read-only (#91) ------------------------------------------------------


def test_repos_lists_this_channels_repos() -> None:
    from jotflow.orchestrator.help import channel_repos

    text = channel_repos(policy(), CH, "")
    assert text.splitlines() == [
        "This channel feeds:",
        "  web: acme-co/web. Web",
        "  api: acme-co/api. API",
    ]
    assert channel_repos(policy(), "C0ELSEWHERE", "") == NOT_SET_UP


@pytest.mark.parametrize("argument", ["add acme-co/mobile", "remove web", "anything"])
def test_changing_repos_says_how_and_changes_nothing(argument: str) -> None:
    from jotflow.orchestrator.help import REPOS_CHANGE, channel_repos

    before = policy()
    assert channel_repos(before, CH, argument) == REPOS_CHANGE
    assert "uvx jotflow init" in REPOS_CHANGE and "GitHub tokens" in REPOS_CHANGE
    assert [r.repo for r in before.channels[CH].repos] == ["acme-co/web", "acme-co/api"]


async def test_the_slash_command_routes_repos() -> None:
    app = FakeApp()
    register_commands(cast(Any, app), policy())
    answers: list[str] = []

    async def ack(answer: str) -> None:
        answers.append(answer)

    await app.commands[SLASH_COMMAND](ack=ack, command={"text": "repos", "channel_id": CH})
    assert answers[0].startswith("This channel feeds:")
