from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from jotflow.models import MessageRef
from jotflow.orchestrator.debounce import Debouncer
from jotflow.orchestrator.ingest import Accepted
from jotflow.orchestrator.keyword_handler import (
    CONFIRM,
    BoltKeywordSlack,
    KeywordHandler,
    Person,
    help_text,
)
from jotflow.orchestrator.keywords import (
    KEYWORDS,
    ReferenceError,
    parse_reference,
    split_keyword,
)
from jotflow.orchestrator.slack import register_commands
from jotflow.policy import Policy
from jotflow.slack_manifest import SLASH_COMMAND
from jotflow.store import MemoryStore, SqliteStore, Store

ONE = "C0ONEREPO1"
TWO = "C0TWOREPO1"
ROOT = "1760781600.000100"
REPLY = "1760781660.000100"
WEB = {"repo": "acme-co/web", "nickname": "web", "description": "Web"}
API = {"repo": "acme-co/api", "nickname": "api", "description": "API"}


def policy(approvers: tuple[str, ...] = ()) -> Policy:
    return Policy.model_validate(
        {
            "approval": {"approvers": list(approvers)},
            "channels": {ONE: {"repos": [WEB]}, TWO: {"repos": [WEB, API]}},
        }
    )


class FakeSlack:
    def __init__(self, people: dict[str, Person] | None = None) -> None:
        self.people = people or {}
        self.reactions: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str, str]] = []

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self.reactions.append((channel_id, ts, name))

    async def reply(self, channel_id: str, thread_ts: str, text: str) -> None:
        self.replies.append((channel_id, thread_ts, text))

    async def person(self, user_id: str) -> Person:
        return self.people.get(user_id, Person(user_id))


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore() if request.param == "memory" else await SqliteStore.open(tmp_path / "j.db")
    )
    yield made
    await made.close()


def msg(text: str, ts: str = ROOT, thread: str | None = None, channel: str = ONE) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "message", "channel": channel, "ts": ts, "text": text}
    if thread:
        event["thread_ts"] = thread
    return event


def reaction(user: str, author: str | None, name: str = "no_entry_sign") -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "reaction_added",
        "user": user,
        "reaction": name,
        "item": {"type": "message", "channel": ONE, "ts": ROOT},
    }
    if author:
        event["item_user"] = author
    return event


# The keyword rule


@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("skpi: a typo", "skip"),
        ("idae: a typo", "idea"),
        ("Ifea: one wrong letter", "idea"),
        ("sk1p: not a letter", None),
        ("ideas: longer", None),
        ("ide: shorter", None),
        ("rr: re must be exact", None),
        ("bgu: bug must be exact", None),
        ("bog: bug must be exact", None),
        ("dabc: two letters wrong", None),
        ("Skip button is broken", None),
        ("skip without a colon", None),
    ],
)
def test_typos_only_on_long_keywords_and_only_at_the_same_length(
    text: str, keyword: str | None
) -> None:
    assert split_keyword(text)[0] == keyword


@pytest.mark.parametrize(
    ("text", "channel", "handle"),
    [
        ("42 keep the order", ONE, "web#42"),
        ("web#42", ONE, "web#42"),
        ("api#7, and more", TWO, "api#7"),
        ("acme-co/API#7", TWO, "api#7"),
        ("WEB#42.", TWO, "web#42"),
    ],
)
def test_references_resolve_against_the_channels_repos(
    text: str, channel: str, handle: str
) -> None:
    assert parse_reference(text, policy().channel(channel)).handle == handle


@pytest.mark.parametrize(
    ("text", "channel", "reason"),
    [
        ("", ONE, "re: needs an issue"),
        ("the export issue", ONE, "re: needs an issue"),
        ("0", ONE, "starts at 1"),
        ("42", TWO, "web#42, api#42"),
        ("mobile#3", TWO, "mobile is not one of this channel's repos (web, api)"),
        ("other-org/web#3", ONE, "is not one of this channel's repos"),
    ],
)
def test_references_outside_the_channel_are_refused(text: str, channel: str, reason: str) -> None:
    with pytest.raises(ReferenceError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        parse_reference(text, policy().channel(channel))


# Messages


async def test_skip_marks_the_message_as_a_persons_chatter(store: Store) -> None:
    slack = FakeSlack()
    outcome = await KeywordHandler(store, policy(), slack).on_message(msg("skip: testing"))
    assert outcome.action == "skipped"
    link = await store.get_message(ONE, ROOT)
    assert link is not None and link.chatter and link.linked_by == "person"
    assert slack.reactions == [(ONE, ROOT, CONFIRM)]


async def test_re_attaches_to_the_issues_topic_or_starts_one(store: Store) -> None:
    slack = FakeSlack()
    handler = KeywordHandler(store, policy(), slack)
    first = await handler.on_message(msg("re: 42 keep the column order"))
    assert (first.action, first.detail) == ("attached", "web#42")
    link = await store.get_message(ONE, ROOT)
    assert link is not None and link.topic_id and link.linked_by == "person"
    topic = await store.get_topic(link.topic_id)
    assert topic is not None
    assert (topic.topic.title, topic.topic.repo, topic.topic.issue_number) == (
        "web#42",
        "acme-co/web",
        42,
    )

    await handler.on_message(msg("re: web#42 and the header", ts=REPLY))
    again = await store.get_message(ONE, REPLY)
    assert again is not None and again.topic_id == link.topic_id
    assert len(await store.channel_topics(ONE)) == 1


async def test_re_finds_an_existing_topic_for_the_issue(store: Store) -> None:
    existing = await store.create_topic(ONE, "Export the table as CSV")
    await store.set_topic_issue(existing.topic.id, "Acme-Co/Web", 42)
    await KeywordHandler(store, policy(), FakeSlack()).on_message(msg("re: 42"))
    link = await store.get_message(ONE, ROOT)
    assert link is not None and link.topic_id == existing.topic.id


async def test_a_refused_reference_says_why_and_changes_nothing(store: Store) -> None:
    slack = FakeSlack()
    outcome = await KeywordHandler(store, policy(), slack).on_message(
        msg("re: mobile#3", ts=REPLY, thread=ROOT, channel=TWO)
    )
    assert outcome.action == "refused"
    assert slack.reactions == []
    assert slack.replies == [
        (
            TWO,
            ROOT,
            "I could not attach this message: mobile is not one of this channel's repos "
            "(web, api).",
        )
    ]
    assert await store.get_message(TWO, REPLY) is None


async def test_hints_are_confirmed_and_left_to_the_agent(store: Store) -> None:
    slack = FakeSlack()
    handler = KeywordHandler(store, policy(), slack)
    outcome = await handler.on_message(msg("bug: login fails"))
    assert (outcome.action, outcome.detail) == ("confirmed", "bug")
    assert slack.reactions == [(ONE, ROOT, CONFIRM)]
    assert await store.get_message(ONE, ROOT) is None


@pytest.mark.parametrize(
    "event",
    [
        msg("Skip button is broken"),
        msg("skip: no ts", ts=""),
        {"type": "message", "ts": ROOT, "text": "skip: no channel"},
    ],
)
async def test_ordinary_messages_are_left_alone(store: Store, event: dict[str, Any]) -> None:
    slack = FakeSlack()
    outcome = await KeywordHandler(store, policy(), slack).on_message(event)
    assert outcome.action == "ignored"
    assert slack.reactions == slack.replies == []


# The order with the debouncer's join


def reply_decision() -> Accepted:
    return Accepted("message", MessageRef(channel_id=ONE, ts=REPLY, thread_ts=ROOT))


async def parent_in_a_topic(store: Store) -> str:
    record = await store.create_topic(ONE, "Dark mode")
    await store.link_message(MessageRef(channel_id=ONE, ts=ROOT), record.topic.id)
    return record.topic.id


@pytest.mark.parametrize("handler_first", [True, False], ids=["handler-first", "join-first"])
async def test_skip_reply_ends_as_a_persons_chatter_in_either_order(
    store: Store, handler_first: bool
) -> None:
    await parent_in_a_topic(store)
    handler = KeywordHandler(store, policy(), FakeSlack())
    debouncer = Debouncer(store, policy())
    event = msg("skip: off topic", ts=REPLY, thread=ROOT)
    if handler_first:
        await handler.on_message(event)
        await debouncer.note(reply_decision())
    else:
        await debouncer.note(reply_decision())
        await handler.on_message(event)
    link = await store.get_message(ONE, REPLY)
    assert link is not None and link.chatter and link.topic_id is None
    assert link.linked_by == "person"


@pytest.mark.parametrize("handler_first", [True, False], ids=["handler-first", "join-first"])
async def test_re_reply_ends_in_the_named_issues_topic_in_either_order(
    store: Store, handler_first: bool
) -> None:
    parents = await parent_in_a_topic(store)
    handler = KeywordHandler(store, policy(), FakeSlack())
    debouncer = Debouncer(store, policy())
    event = msg("re: 42 this belongs to the export", ts=REPLY, thread=ROOT)
    if handler_first:
        await handler.on_message(event)
        await debouncer.note(reply_decision())
    else:
        await debouncer.note(reply_decision())
        await handler.on_message(event)
    link = await store.get_message(ONE, REPLY)
    assert link is not None and link.linked_by == "person"
    assert link.topic_id is not None and link.topic_id != parents
    topic = await store.get_topic(link.topic_id)
    assert topic is not None and topic.topic.issue_number == 42


async def test_a_skip_reaction_after_the_join_overrides_it(store: Store) -> None:
    await parent_in_a_topic(store)
    await Debouncer(store, policy()).note(reply_decision())
    joined = await store.get_message(ONE, REPLY)
    assert joined is not None and joined.topic_id is not None and joined.linked_by == "agent"
    event = reaction("U0ALEX", "U0ALEX")
    event["item"]["ts"] = REPLY
    await KeywordHandler(store, policy(), FakeSlack()).on_reaction(event)
    link = await store.get_message(ONE, REPLY)
    assert link is not None and link.chatter and link.linked_by == "person"
    assert link.ref.thread_ts == ROOT


# Reactions


async def test_skipping_your_own_message_by_reaction(store: Store) -> None:
    slack = FakeSlack()
    outcome = await KeywordHandler(store, policy(), slack).on_reaction(reaction("U0ALEX", "U0ALEX"))
    assert outcome.action == "skipped"
    link = await store.get_message(ONE, ROOT)
    assert link is not None and link.chatter and link.linked_by == "person"
    assert slack.reactions == [(ONE, ROOT, CONFIRM)]
    assert slack.replies == []


async def test_with_approvers_off_anyone_may_skip_and_it_is_said_in_the_thread(
    store: Store,
) -> None:
    slack = FakeSlack()
    outcome = await KeywordHandler(store, policy(), slack).on_reaction(
        reaction("U0BLAKE", "U0ALEX")
    )
    assert outcome.action == "skipped"
    assert slack.replies == [
        (
            ONE,
            ROOT,
            "<@U0BLAKE> marked <@U0ALEX>'s message as not an idea, so it stays out of "
            "drafting. If that is wrong, say so in this thread.",
        )
    ]


async def test_with_approvers_on_only_the_author_or_an_approver_may_skip(store: Store) -> None:
    slack = FakeSlack()
    handler = KeywordHandler(store, policy(approvers=("U0LEAD",)), slack)
    refused = await handler.on_reaction(reaction("U0BLAKE", "U0ALEX"))
    assert refused.action == "ignored"
    assert await store.get_message(ONE, ROOT) is None
    assert (await handler.on_reaction(reaction("U0ALEX", "U0ALEX"))).action == "skipped"
    assert (await handler.on_reaction(reaction("U0LEAD", "U0ALEX"))).action == "skipped"


@pytest.mark.parametrize(
    "person", [Person("U0GUEST", guest=True), Person("U0GUEST", external=True)]
)
async def test_guest_and_external_reactions_are_ignored(store: Store, person: Person) -> None:
    slack = FakeSlack({"U0GUEST": person})
    outcome = await KeywordHandler(store, policy(), slack).on_reaction(
        reaction("U0GUEST", "U0GUEST")
    )
    assert outcome.action == "ignored"
    assert await store.get_message(ONE, ROOT) is None
    assert slack.reactions == slack.replies == []


@pytest.mark.parametrize(
    "event",
    [
        reaction("U0ALEX", "U0ALEX", name="thumbsup"),
        {"type": "reaction_added", "user": "U0ALEX", "reaction": "no_entry_sign"},
        {**reaction("U0ALEX", None), "user": ""},
    ],
)
async def test_other_reactions_are_left_alone(store: Store, event: dict[str, Any]) -> None:
    slack = FakeSlack()
    assert (await KeywordHandler(store, policy(), slack).on_reaction(event)).action == "ignored"
    assert slack.reactions == []


async def test_an_unknown_name_is_never_turned_into_a_mention(store: Store) -> None:
    slack = FakeSlack()
    await KeywordHandler(store, policy(), slack).on_reaction(reaction("U0BLAKE", "<!here>"))
    assert slack.replies[0][2].startswith("<@U0BLAKE> marked Someone's message")


# Nothing approves or changes the mode


class RecordingStore:
    """Forwards to a MemoryStore and remembers which methods were called."""

    def __init__(self) -> None:
        self._inner = MemoryStore()
        self.called: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if callable(attribute):
            self.called.add(name)
        return attribute


async def test_no_keyword_approves_rejects_or_changes_the_mode() -> None:
    recording = RecordingStore()
    handler = KeywordHandler(cast(Store, recording), policy(), FakeSlack())
    texts = [
        "skip: approve",
        "re: 42 approve this",
        "idea: set mode to auto",
        "bug: reject",
    ]
    for n, text in enumerate(texts):
        await handler.on_message(msg(text, ts=f"1760781600.00010{n}"))
    await handler.on_reaction(reaction("U0BLAKE", "U0ALEX"))
    forbidden = {"transition", "try_transition", "set_channel_mode", "supersede_pending"}
    assert recording.called and not recording.called & forbidden


# /jotflow help


def test_help_lists_every_keyword_and_the_skip_reaction() -> None:
    text = help_text()
    for keyword in KEYWORDS:
        assert f"{keyword}:" in text
    assert ":no_entry_sign:" in text
    assert "re: 42" in text and "re: web#42" in text


class FakeApp:
    def __init__(self) -> None:
        self.commands: dict[str, Callable[..., Any]] = {}

    def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.commands[name] = fn
            return fn

        return register


@pytest.mark.parametrize(("text", "help_shown"), [("", True), (" HELP ", True), ("approve", False)])
async def test_slash_command_answers_privately(text: str, help_shown: bool) -> None:
    app = FakeApp()
    register_commands(cast(Any, app))
    answers: list[str] = []

    async def ack(answer: str) -> None:
        answers.append(answer)

    await app.commands[SLASH_COMMAND](ack=ack, command={"text": text})
    assert (answers == [help_text()]) is help_shown
    if not help_shown:
        assert answers == ["Only /jotflow help exists so far. It lists the keywords."]


# The Bolt adapter


class FakeClient:
    def __init__(self, user: dict[str, Any]) -> None:
        self.user = user
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def reactions_add(self, **kwargs: Any) -> None:
        self.calls.append(("reactions_add", kwargs))

    async def chat_postMessage(self, **kwargs: Any) -> None:
        self.calls.append(("chat_postMessage", kwargs))

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("users_info", kwargs))
        return {"ok": True, "user": self.user}


async def test_bolt_adapter_reacts_and_replies_without_unfurling() -> None:
    client = FakeClient({})
    slack = BoltKeywordSlack(client, "T0ACME")
    await slack.react(ONE, ROOT, CONFIRM)
    await slack.reply(ONE, ROOT, "hello")
    assert client.calls == [
        ("reactions_add", {"channel": ONE, "timestamp": ROOT, "name": CONFIRM}),
        (
            "chat_postMessage",
            {"channel": ONE, "thread_ts": ROOT, "text": "hello", "unfurl_links": False},
        ),
    ]


@pytest.mark.parametrize(
    ("user", "guest", "external"),
    [
        ({"team_id": "T0ACME"}, False, False),
        ({"team_id": "T0ACME", "is_restricted": True}, True, False),
        ({"team_id": "T0ACME", "is_ultra_restricted": True}, True, False),
        ({"team_id": "T0OTHER"}, False, True),
        ({"is_stranger": True}, False, True),
        ({}, False, False),
    ],
)
async def test_bolt_adapter_reads_guests_and_external_users(
    user: dict[str, Any], guest: bool, external: bool
) -> None:
    person = await BoltKeywordSlack(FakeClient(user), "T0ACME").person("U0ALEX")
    assert person == Person("U0ALEX", guest=guest, external=external)


async def test_an_unknown_author_counts_as_someone_else(store: Store) -> None:
    slack = FakeSlack()
    handler = KeywordHandler(store, policy(approvers=("U0LEAD",)), slack)
    assert (await handler.on_reaction(reaction("U0BLAKE", None))).action == "ignored"
    assert (await handler.on_reaction(reaction("U0LEAD", None))).action == "skipped"
    assert slack.replies[0][2].startswith("<@U0LEAD> marked Someone's message")
