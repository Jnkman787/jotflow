"""`/jotflow link`: each person links only their own GitHub login (#35)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import pytest

from jotflow.orchestrator.identity import (
    NOT_A_LOGIN,
    NOT_A_MEMBER_ID,
    NOT_ALLOWED,
    NOT_AN_ADMIN,
    NOTHING_LINKED,
    TAKEN,
    LinkCommand,
    SlackNote,
    clean_login,
    removal_note,
)
from jotflow.orchestrator.people import Person
from jotflow.orchestrator.slack import register_commands
from jotflow.policy import parse_policy
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


# Removing someone else's link (#173)

APPROVER, ADMIN, MALLORY = "U0APPROVE", "U0ADMIN", "U0MALLORY"
POLICY = parse_policy(
    f"""
[approval]
approvers = ["{APPROVER}"]

[identity]
admins = ["{ADMIN}"]

[channels.C0IDEAS01]

[[channels.C0IDEAS01.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"
"""
)


class Names:
    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.names = names or {APPROVER: "Avery", ADMIN: "Ada", BOB: "Bob"}

    async def name(self, user_id: str) -> str | None:
        return self.names.get(user_id)


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.broken = False

    async def tell(self, user_id: str, text: str) -> None:
        if self.broken:
            raise RuntimeError("messages tab off")
        self.sent.append((user_id, text))


def admin_command(
    policy: Any = POLICY, names: Names | None = None
) -> tuple[LinkCommand, MemoryStore, People, Notes]:
    store, people, notes = MemoryStore(), People(), Notes()
    links = LinkCommand(store, people, policy=policy, names=names or Names(), notifier=notes)
    return links, store, people, notes


@pytest.mark.parametrize("actor", [APPROVER, ADMIN])
async def test_an_approver_or_admin_unlinks_someone_and_they_are_told(
    actor: str, caplog: pytest.LogCaptureFixture
) -> None:
    links, store, _, notes = admin_command()
    await store.link_identity(BOB, "octo-cat")
    with caplog.at_level(logging.INFO, logger="jotflow.identity"):
        reply = await links.handle(actor, "unlink", f"  {BOB} ")
    assert reply.text.startswith(f"Unlinked the GitHub login of Bob ({BOB}).")
    assert "told them privately" in reply.text
    assert await store.get_identity(BOB) is None
    [(told, note)] = notes.sent
    assert told == BOB
    assert note.startswith(("Avery", "Ada"))
    assert "/jotflow link" in note
    [record] = await store.identity_unlinks()
    assert (record.slack_user_id, record.removed_by) == (BOB, actor)
    assert "octo-cat" not in caplog.text
    assert "octo-cat" not in note


@pytest.mark.parametrize("typed", [f"<@{BOB}>", f"<@{BOB}|bob>", f"  {BOB}"])
async def test_the_target_may_be_an_id_or_a_mention(typed: str) -> None:
    links, store, _, _ = admin_command()
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(APPROVER, "unlink", typed)).text.startswith("Unlinked")
    assert await store.get_identity(BOB) is None


@pytest.mark.parametrize(
    "typed", ["@bob", "bob", "uma", "u0bob", "Bob Smith", "<@bob>", "C0IDEAS01", f"<@{BOB}"]
)
async def test_a_name_is_never_resolved(typed: str) -> None:
    links, store, _, notes = admin_command()
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(APPROVER, "unlink", typed)).text == NOT_A_MEMBER_ID
    assert await store.get_identity(BOB) is not None
    assert notes.sent == []


async def test_anyone_else_is_refused() -> None:
    links, store, _, notes = admin_command()
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(MALLORY, "unlink", BOB)).text == NOT_AN_ADMIN
    assert await store.get_identity(BOB) is not None
    assert notes.sent == [] and await store.identity_unlinks() == []


async def test_with_no_approvers_or_admins_only_the_host_can_unlink_others() -> None:
    open_policy = parse_policy(
        """
[channels.C0IDEAS01]

[[channels.C0IDEAS01.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The customer-facing website"
"""
    )
    links, store, _, _ = admin_command(open_policy)
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(ALICE, "unlink", BOB)).text == NOT_AN_ADMIN


@pytest.mark.parametrize("person", [Person(APPROVER, guest=True), Person(APPROVER, external=True)])
async def test_a_guest_or_external_approver_id_is_refused(person: Person) -> None:
    links, store, people, _ = admin_command()
    people.people[APPROVER] = person
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(APPROVER, "unlink", BOB)).text == NOT_ALLOWED
    assert await store.get_identity(BOB) is not None


async def test_a_failed_lookup_of_the_actor_refuses() -> None:
    links, store, people, _ = admin_command()
    people.broken = True
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(APPROVER, "unlink", BOB)).text == NOT_ALLOWED


async def test_a_mistyped_id_is_obvious_from_the_reply() -> None:
    links, _, _, notes = admin_command()
    reply = await links.handle(APPROVER, "unlink", "U0NOBODY")
    assert reply.text == "an unknown user (U0NOBODY) has no linked GitHub login."
    assert notes.sent == []


async def test_a_name_with_markup_is_shown_plainly() -> None:
    names = Names({BOB: "<!channel> Bob & <@U0X>", APPROVER: "<b>Avery</b>"})
    links, store, _, notes = admin_command(names=names)
    await store.link_identity(BOB, "octo-cat")
    reply = await links.handle(APPROVER, "unlink", BOB)
    assert "<" not in reply.text and "&" not in reply.text
    [(_, note)] = notes.sent
    assert "<" not in note


async def test_if_the_note_fails_the_actor_is_asked_to_tell_them() -> None:
    links, store, _, notes = admin_command()
    notes.broken = True
    await store.link_identity(BOB, "octo-cat")
    reply = await links.handle(APPROVER, "unlink", BOB)
    assert "couldn't message them" in reply.text
    assert await store.get_identity(BOB) is None
    assert len(await store.identity_unlinks()) == 1


@pytest.mark.parametrize("typed", [MALLORY, f"<@{MALLORY}>", f"<@{MALLORY}|mallory>"])
async def test_naming_yourself_is_a_plain_self_unlink_and_is_not_recorded(typed: str) -> None:
    """Anyone who may self-unlink may name themselves; it's not "someone else"."""
    links, store, _, notes = admin_command()
    await store.link_identity(MALLORY, "octo-cat")
    reply = await links.handle(MALLORY, "unlink", typed)
    assert reply.text.startswith("Unlinked GitHub login octo-cat")
    assert notes.sent == [] and await store.identity_unlinks() == []


async def test_without_a_notifier_unlinking_others_is_off() -> None:
    store, people = MemoryStore(), People()
    links = LinkCommand(store, people, policy=POLICY)
    await store.link_identity(BOB, "octo-cat")
    assert (await links.handle(APPROVER, "unlink", BOB)).text == NOT_AN_ADMIN


async def test_the_slack_note_is_plain_text_without_unfurls() -> None:
    class Client:
        def __init__(self) -> None:
            self.posts: list[dict[str, Any]] = []

        async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            self.posts.append(kwargs)
            return {"ok": True}

    client = Client()
    await SlackNote(client).tell(BOB, removal_note(None))
    [post] = client.posts
    assert post["channel"] == BOB
    assert post["blocks"][0]["text"]["type"] == "plain_text"
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert post["text"].startswith("Whoever runs JotFlow removed")


async def test_the_command_passes_the_member_id_unchanged() -> None:
    links, store, _, notes = admin_command()
    await store.link_identity(BOB, "octo-cat")
    app = App()
    register_commands(cast(Any, app), links=links)
    said: list[str] = []

    async def ack(*args: Any) -> None:
        return None

    async def respond(text: str) -> None:
        said.append(text)

    sent = {"user_id": APPROVER, "channel_id": "C0ANY", "text": f"Unlink {BOB}"}
    await app.commands[SLASH_COMMAND](ack=ack, command=sent, respond=respond)
    assert said[0].startswith(f"Unlinked the GitHub login of Bob ({BOB})")
    assert [user for user, _ in notes.sent] == [BOB]
