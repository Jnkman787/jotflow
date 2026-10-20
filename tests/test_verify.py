"""`/jotflow verify`: proving control of a GitHub login with a one-time code (#195)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from jotflow.adapters.github_proof import code_digest
from jotflow.orchestrator import verify as v
from jotflow.orchestrator.identity import NOT_ALLOWED
from jotflow.orchestrator.launch import RunnerError, VerifyAnswer
from jotflow.orchestrator.people import Person
from jotflow.orchestrator.slack import register_commands
from jotflow.slack_manifest import SLASH_COMMAND
from jotflow.store import MemoryStore

BOB, SQUAT = "U0BOB", "U0SQUAT"
CODE = "jotflow-Ab3_xY9-kLm2"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class People:
    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self.broken = False

    async def person(self, user_id: str) -> Person:
        if self.broken:
            raise RuntimeError("slack is down")
        return self.people.get(user_id, Person(user_id))


class Runner:
    def __init__(self, answer: VerifyAnswer | None = None, *, fails: bool = False) -> None:
        self.answer = answer or VerifyAnswer(verified=False, rate_limited=False)
        self.fails = fails
        self.asked: list[tuple[str, str]] = []

    async def verify(self, login: str, digest: str) -> VerifyAnswer:
        self.asked.append((login, digest))
        if self.fails:
            raise RunnerError("the runner could not be reached")
        return self.answer


class Notes:
    def __init__(self, broken: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.broken = broken

    async def tell(self, user_id: str, text: str) -> None:
        if self.broken:
            raise RuntimeError("messages tab off")
        self.sent.append((user_id, text))


def found(login: str = "Octo-Cat") -> VerifyAnswer:
    return VerifyAnswer(verified=True, rate_limited=False, github_id=42, login=login)


class Rig:
    def __init__(self, runner: Runner | None = None, *, on: bool = True) -> None:
        self.store = MemoryStore()
        self.people = People()
        self.runner = runner or Runner()
        self.notes = Notes()
        self.clock, self.mono = Clock(), Mono()
        self.command = v.VerifyCommand(
            self.store,
            self.people,
            (lambda: self.runner) if on else (lambda: None),
            self.notes,
            clock=self.clock,
            monotonic=self.mono,
            code=lambda: CODE,
        )

    async def ask(self, argument: str = "", user: str = BOB) -> str:
        return await self.command.handle(user, argument)


@pytest.mark.parametrize("person", [Person(BOB, guest=True), Person(BOB, external=True)])
async def test_guests_and_external_users_cannot_verify(person: Person) -> None:
    rig = Rig()
    rig.people.people[BOB] = person
    assert await rig.ask() == NOT_ALLOWED
    assert await rig.ask("check") == NOT_ALLOWED


async def test_a_failed_lookup_refuses() -> None:
    rig = Rig()
    rig.people.broken = True
    assert await rig.ask("octo-cat") == NOT_ALLOWED


async def test_without_a_link_it_says_how_to_start() -> None:
    assert await Rig().ask() == v.NO_LINK


async def test_a_code_is_shown_once_and_only_its_digest_is_kept() -> None:
    rig = Rig()
    await rig.store.link_identity(BOB, "octo-cat")
    reply = await rig.ask()
    assert f"`{CODE}`" in reply and "octo-cat" in reply
    pending = await rig.store.get_verification(BOB)
    assert pending is not None
    assert pending.code_digest == code_digest(CODE) and CODE not in repr(pending)
    assert pending.expires_at == rig.clock.now + timedelta(minutes=30)


async def test_without_the_drafting_runner_it_says_so() -> None:
    rig = Rig(on=False)
    await rig.store.link_identity(BOB, "octo-cat")
    assert await rig.ask() == v.NO_RUNNER


async def test_checking_finds_the_code_and_verifies_the_link() -> None:
    rig = Rig(Runner(found()))
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    reply = await rig.ask("check")
    assert reply.startswith("Verified: GitHub login Octo-Cat")
    assert rig.runner.asked == [("octo-cat", code_digest(CODE))]
    link = await rig.store.get_identity(BOB)
    assert link is not None and link.verified and link.github_login == "Octo-Cat"
    assert await rig.store.get_verification(BOB) is None
    assert (await rig.ask()).startswith("Your GitHub login Octo-Cat is already verified")


async def test_a_claim_moves_a_squatted_login_and_tells_the_squatter() -> None:
    rig = Rig(Runner(found("octo-cat")))
    await rig.store.link_identity(SQUAT, "octo-cat")
    await rig.ask("https://github.com/octo-cat")
    assert (await rig.ask("check")).startswith("Verified")
    assert await rig.store.get_identity(SQUAT) is None
    assert rig.notes.sent == [(SQUAT, v.DISPLACED_NOTE)]
    [record] = await rig.store.identity_unlinks()
    assert (record.slack_user_id, record.removed_by) == (SQUAT, "verification")


async def test_a_note_that_fails_doesnt_undo_the_proof() -> None:
    rig = Rig(Runner(found("octo-cat")))
    rig.notes.broken = True
    await rig.store.link_identity(SQUAT, "octo-cat")
    await rig.ask("octo-cat")
    assert (await rig.ask("check")).startswith("Verified")


async def test_no_code_is_counted_and_withdrawn_after_five() -> None:
    rig = Rig()
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    for _ in range(4):
        assert (await rig.ask("check")).startswith("JotFlow didn't find your code")
        rig.mono.now += 31
    assert await rig.ask("check") == v.TOO_MANY
    assert await rig.store.get_verification(BOB) is None


async def test_github_naming_another_login_is_no_proof() -> None:
    rig = Rig(Runner(found("someone-else")))
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    assert (await rig.ask("check")).startswith("JotFlow didn't find your code")
    link = await rig.store.get_identity(BOB)
    assert link is not None and not link.verified


async def test_a_rate_limit_is_try_again_and_doesnt_count() -> None:
    rig = Rig(Runner(VerifyAnswer(verified=False, rate_limited=True)))
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    for _ in range(7):
        assert await rig.ask("check") == v.RATE_LIMITED
        rig.mono.now += 31
    pending = await rig.store.get_verification(BOB)
    assert pending is not None and pending.checks == 0


async def test_a_runner_that_fails_is_try_again() -> None:
    rig = Rig(Runner(fails=True))
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    assert await rig.ask("check") == v.UNAVAILABLE


async def test_checks_are_spaced_out() -> None:
    rig = Rig()
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    await rig.ask("check")
    assert await rig.ask("check") == v.TOO_SOON
    assert len(rig.runner.asked) == 1


async def test_an_expired_code_is_dropped() -> None:
    rig = Rig(Runner(found()))
    await rig.store.link_identity(BOB, "octo-cat")
    await rig.ask()
    rig.clock.now += timedelta(minutes=30)
    assert await rig.ask("check") == v.EXPIRED
    assert rig.runner.asked == [] and await rig.store.get_verification(BOB) is None


async def test_no_code_waiting_and_a_bad_login() -> None:
    rig = Rig()
    assert await rig.ask("check") == v.NO_PENDING
    assert await rig.ask("not a login!") == v.NOT_A_LOGIN


async def test_the_code_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig(Runner(found()))
    await rig.store.link_identity(BOB, "octo-cat")
    with caplog.at_level(logging.DEBUG):
        await rig.ask()
        await rig.ask("check")
    assert CODE not in caplog.text and code_digest(CODE) not in caplog.text


def test_a_new_code_is_the_shape_the_runner_looks_for() -> None:
    from jotflow.adapters.github_proof import CODE as SHAPE

    codes = {v.new_code() for _ in range(50)}
    assert len(codes) == 50 and all(SHAPE.fullmatch(c) for c in codes)


async def test_the_slash_command_routes_verify_privately() -> None:
    rig = Rig()
    await rig.store.link_identity(BOB, "octo-cat")

    class App:
        def __init__(self) -> None:
            self.handler: Any = None

        def command(self, name: str) -> Any:
            def register(fn: Any) -> Any:
                self.handler = fn
                return fn

            assert name == SLASH_COMMAND
            return register

    app = App()
    register_commands(cast(Any, app), verify=rig.command)
    said: list[str] = []

    async def ack(*args: Any) -> None:
        return None

    async def respond(text: str) -> None:
        said.append(text)

    await app.handler(ack=ack, command={"user_id": BOB, "text": "Verify"}, respond=respond)
    assert f"`{CODE}`" in said[0]
