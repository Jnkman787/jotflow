"""`/jotflow link`: each person links only their own GitHub login (#35)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import pytest

from jotflow.orchestrator.identity import (
    NOT_A_LOGIN,
    NOT_ALLOWED,
    NOTHING_LINKED,
    TAKEN,
    LinkCommand,
    clean_login,
)
from jotflow.orchestrator.people import Person
from jotflow.orchestrator.slack import register_commands
from jotflow.slack_manifest import SLASH_COMMAND
from jotflow.store import MemoryStore

ALICE, BOB = "U0ALICE", "U0BOB"


class People:
    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self.broken = False

    async def person(self, user_id: str) -> Person:
        if self.broken:
            raise RuntimeError("slack is down")
        return self.people.get(user_id, Person(user_id))


def command() -> tuple[LinkCommand, MemoryStore, People]:
    store, people = MemoryStore(), People()
    return LinkCommand(store, people), store, people


@pytest.mark.parametrize(
    ("typed", "login"),
    [
        ("octo-cat", "octo-cat"),
        ("  @Octo-Cat ", "Octo-Cat"),
        ("https://github.com/octo-cat", "octo-cat"),
        ("github.com/octo-cat/", "octo-cat"),
        ("a" * 39, "a" * 39),
    ],
)
def test_a_login_is_cleaned_from_what_people_type(typed: str, login: str) -> None:
    assert clean_login(typed) == login


@pytest.mark.parametrize(
    "typed",
    ["", "-octo", "octo-", "oc--to", "a" * 40, "octo cat", "<@U0EVIL>", "github.com/a/b", "o_c"],
)
def test_anything_else_is_not_a_login(typed: str) -> None:
    assert clean_login(typed) is None


async def test_link_view_and_unlink_your_own_login(caplog: pytest.LogCaptureFixture) -> None:
    links, store, _ = command()
    with caplog.at_level(logging.INFO, logger="jotflow.identity"):
        assert (await links.handle(ALICE, "link", "")).text == NOTHING_LINKED
        linked = await links.handle(ALICE, "link", "@Octo-Cat")
        assert "Linked to GitHub login Octo-Cat" in linked.text and "unverified" in linked.text
        stored = await store.get_identity(ALICE)
        assert stored is not None and (stored.github_login, stored.verified) == ("Octo-Cat", False)
        assert "linked to GitHub login Octo-Cat" in (await links.handle(ALICE, "link", "")).text
        assert "Unlinked GitHub login Octo-Cat" in (await links.handle(ALICE, "unlink", "")).text
        assert await store.get_identity(ALICE) is None
        assert (await links.handle(ALICE, "unlink", "")).text == NOTHING_LINKED
    # Ids and actions only: never the login.
    assert "Octo-Cat" not in caplog.text and "user U0ALICE linked a GitHub login" in caplog.text


async def test_relinking_replaces_your_own_login() -> None:
    links, store, _ = command()
    await links.handle(ALICE, "link", "first-login")
    await links.handle(ALICE, "link", "second-login")
    stored = await store.get_identity(ALICE)
    assert stored is not None and stored.github_login == "second-login"


async def test_a_login_someone_else_holds_is_refused_without_naming_them() -> None:
    links, store, _ = command()
    await links.handle(ALICE, "link", "octo-cat")
    reply = await links.handle(BOB, "link", "OCTO-CAT")
    assert reply.text == TAKEN and ALICE not in reply.text
    assert await store.get_identity(BOB) is None
    stored = await store.get_identity(ALICE)
    assert stored is not None and stored.github_login == "octo-cat"


async def test_a_bad_login_is_refused() -> None:
    links, store, _ = command()
    assert (await links.handle(ALICE, "link", "-not-a-login")).text == NOT_A_LOGIN
    assert await store.get_identity(ALICE) is None


@pytest.mark.parametrize("person", [Person(ALICE, guest=True), Person(ALICE, external=True)])
async def test_guests_and_external_users_cannot_link_or_unlink(person: Person) -> None:
    links, store, people = command()
    await store.link_identity(ALICE, "octo-cat")  # say, linked before they became a guest
    people.people[ALICE] = person
    for verb, argument in (("link", "other-login"), ("link", ""), ("unlink", "")):
        assert (await links.handle(ALICE, verb, argument)).text == NOT_ALLOWED
    stored = await store.get_identity(ALICE)
    assert stored is not None and stored.github_login == "octo-cat"


async def test_a_failed_lookup_refuses() -> None:
    links, store, people = command()
    people.broken = True
    assert (await links.handle(ALICE, "link", "octo-cat")).text == NOT_ALLOWED
    assert await store.get_identity(ALICE) is None


# Routing through /jotflow


class App:
    def __init__(self) -> None:
        self.commands: dict[str, Callable[..., Any]] = {}

    def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.commands[name] = fn
            return fn

        return register


async def test_the_command_links_only_the_caller_and_answers_privately() -> None:
    links, store, _ = command()
    app = App()
    register_commands(cast(Any, app), links=links)
    acks: list[Any] = []
    said: list[str] = []

    async def ack(*args: Any) -> None:
        acks.append(args)

    async def respond(text: str) -> None:
        said.append(text)

    # The argument names a login, never a user: <@U0BOB> is just not a login.
    sent = {"user_id": ALICE, "channel_id": "C0ANY", "text": "link  Octo-Cat"}
    await app.commands[SLASH_COMMAND](ack=ack, command=sent, respond=respond)
    await app.commands[SLASH_COMMAND](
        ack=ack, command={"user_id": ALICE, "text": "link <@U0BOB>"}, respond=respond
    )
    await app.commands[SLASH_COMMAND](
        ack=ack, command={"user_id": ALICE, "text": "UNLINK"}, respond=respond
    )
    assert acks == [(), (), ()]  # acknowledged first, answered privately after
    assert said[0].startswith("Linked to GitHub login Octo-Cat")  # case kept
    assert said[1] == NOT_A_LOGIN
    assert said[2].startswith("Unlinked GitHub login Octo-Cat")
    assert await store.get_identity(ALICE) is None and await store.get_identity(BOB) is None


async def test_without_links_the_command_falls_back_to_the_hint() -> None:
    app = App()
    register_commands(cast(Any, app))
    answers: list[str] = []

    async def ack(answer: str = "") -> None:
        answers.append(answer)

    await app.commands[SLASH_COMMAND](ack=ack, command={"text": "link octo-cat"})
    assert "/jotflow link your-github-login" in answers[0]
