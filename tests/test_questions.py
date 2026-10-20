"""Questions asked with ``ask:`` or an @-mention, and how they're answered (#57)."""

from __future__ import annotations

from typing import Any

import pytest

from jotflow.models import ChannelMode, RunKind
from jotflow.orchestrator.people import Person
from jotflow.orchestrator.questions import (
    MEMBERS_ONLY,
    OFF,
    TOO_MANY,
    TOO_SOON,
    QuestionHandler,
    is_question,
    mentions_bot,
)
from jotflow.policy import Policy
from jotflow.store import MemoryStore
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import (
    ANSWER_PREFIX,
    AnswerArgs,
    WriteTools,
    safe_text,
)
from tests.test_write_tools import CH, ROOT, Clock, LiveStore, Poster

TEAM = "T0OURS"
BOT = "U0JOTFLOW"
ALICE = "U0ALICE"


def policy(**questions: Any) -> Policy:
    data: dict[str, Any] = {
        "channels": {
            CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "W"}]}
        },
    }
    if questions:
        data["questions"] = questions
    return Policy.model_validate(data)


class Slack:
    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self.whispers: list[tuple[str, str, str | None, str]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.team: str | None = TEAM
        self.lookup_fails = False

    async def person(self, user_id: str) -> Person:
        if self.lookup_fails:
            raise RuntimeError("users.info failed")
        return self.people.get(user_id, Person(user_id))

    async def own_team(self) -> str | None:
        return self.team

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        self.whispers.append((channel_id, user_id, thread_ts, text))

    async def react(self, channel_id: str, ts: str, name: str) -> None:
        self.reactions.append((channel_id, ts, name))


class Ticker:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def event(text: str, *, ts: str = "1.1", user: str = ALICE, **extra: Any) -> dict[str, Any]:
    return {"type": "message", "channel": CH, "ts": ts, "user": user, "text": text, **extra}


def handler(
    store: MemoryStore | None = None,
    *,
    slack: Slack | None = None,
    ticker: Ticker | None = None,
    **questions: Any,
) -> tuple[QuestionHandler, MemoryStore, Slack, Ticker]:
    store = store or MemoryStore()
    slack = slack or Slack()
    ticker = ticker or Ticker()
    return (
        QuestionHandler(store, policy(**questions), slack, BOT, monotonic=ticker),
        store,
        slack,
        ticker,
    )


# --- what counts as a question ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("ask: is CSV export tracked?", True),
        ("Ask: anything", True),
        (f"<@{BOT}> is this tracked?", True),
        (f"hey <@{BOT}|jotflow> quick one", True),
        ("<@U0SOMEONE> is this tracked?", False),
        ("<!channel> anyone?", False),
        ("<!here> ask: no", False),
        ("ask without a colon", False),
        ("I'd ask: later", False),
    ],
)
def test_what_counts_as_a_question(text: str, asked: bool) -> None:
    assert is_question(text, BOT) is asked


def test_only_a_real_user_id_can_be_the_bot() -> None:
    assert not mentions_bot("<@>", "")
    assert not mentions_bot("<@U0X>", "not-an-id")


# --- queueing ---------------------------------------------------------------------------------


async def test_a_question_queues_a_run_marks_chatter_and_is_acknowledged() -> None:
    questions, store, slack, _ = handler()
    asked = await questions.on_message(event("ask: is CSV export tracked?", thread_ts=ROOT))
    assert asked.outcome == "queued"
    runs = await store.thread_runs(CH, ROOT)
    assert [r.kind for r in runs] == [RunKind.QUESTION]
    link = await store.get_message(CH, "1.1")
    # The agent's mark, not a person's, so the thread's ideas still draft.
    assert link is not None and link.chatter and link.linked_by == "agent"
    assert slack.reactions == [(CH, "1.1", "white_check_mark")]
    assert slack.whispers == []


async def test_a_top_level_question_is_its_own_thread() -> None:
    questions, store, _, _ = handler()
    await questions.on_message(event(f"<@{BOT}> how do I skip a message?", ts="2.0"))
    assert [r.thread_ts for r in await store.thread_runs(CH, "2.0")] == ["2.0"]


async def test_a_message_that_isnt_a_question_does_nothing() -> None:
    questions, store, slack, _ = handler()
    assert (await questions.on_message(event("a progress bar please"))).outcome == "not_a_question"
    assert await store.thread_runs(CH, "1.1") == []
    assert slack.whispers == [] and slack.reactions == []


async def test_a_channel_jotflow_doesnt_watch_is_not_a_question() -> None:
    questions, _, _, _ = handler()
    asked = await questions.on_message({**event("ask: hi"), "channel": "C0NOTOURS"})
    assert asked.outcome == "not_a_question"


# --- who may ask ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup",
    ["guest", "external", "lookup_fails", "no_team", "user_team", "source_team", "bad_user"],
)
async def test_only_this_workspaces_members_may_ask(setup: str) -> None:
    slack = Slack()
    extra: dict[str, Any] = {}
    user = ALICE
    if setup == "guest":
        slack.people[ALICE] = Person(ALICE, guest=True)
    elif setup == "external":
        slack.people[ALICE] = Person(ALICE, external=True)
    elif setup == "lookup_fails":
        slack.lookup_fails = True
    elif setup == "no_team":
        slack.team = None
    elif setup == "user_team":
        extra["user_team"] = "T0THEIRS"
    elif setup == "source_team":
        extra["source_team"] = "T0THEIRS"
    elif setup == "bad_user":
        user = "not-a-user"
    questions, store, _, _ = handler(slack=slack)
    asked = await questions.on_message(
        event("ask: what's in web's security issues?", user=user, **extra)
    )
    assert asked.outcome == "refused"
    assert await store.thread_runs(CH, "1.1") == []
    if setup != "bad_user":
        assert [w[3] for w in slack.whispers] == [MEMBERS_ONLY]


# --- limits -----------------------------------------------------------------------------------


async def test_one_person_waits_between_questions() -> None:
    questions, _, slack, ticker = handler(per_person_seconds=60)
    assert (await questions.on_message(event("ask: one", ts="1.1"))).outcome == "queued"
    assert (await questions.on_message(event("ask: two", ts="1.2"))).outcome == "limited"
    assert slack.whispers[-1][3] == TOO_SOON
    ticker.now += 61
    assert (await questions.on_message(event("ask: three", ts="1.3"))).outcome == "queued"


async def test_a_channel_has_an_hourly_limit() -> None:
    questions, _, slack, _ = handler(per_hour=2, per_person_seconds=0)
    for n, ts in enumerate(("1.1", "1.2"), 1):
        asked = await questions.on_message(event(f"ask: {n}", ts=ts, user=f"U0P{n}"))
        assert asked.outcome == "queued"
    asked = await questions.on_message(event("ask: 3", ts="1.3", user="U0P3"))
    assert asked.outcome == "limited" and slack.whispers[-1][3] == TOO_MANY


async def test_the_private_reply_itself_is_limited() -> None:
    slack = Slack()
    slack.people[ALICE] = Person(ALICE, guest=True)
    questions, _, _, ticker = handler(slack=slack)
    for ts in ("1.1", "1.2", "1.3"):
        await questions.on_message(event("ask: hi", ts=ts))
    assert len(slack.whispers) == 1
    ticker.now += 601
    await questions.on_message(event("ask: hi", ts="1.4"))
    assert len(slack.whispers) == 2


async def test_questions_can_be_turned_off() -> None:
    questions, store, slack, _ = handler(enabled=False)
    assert (await questions.on_message(event("ask: hi"))).outcome == "off"
    assert await store.thread_runs(CH, "1.1") == [] and slack.whispers[-1][3] == OFF


def test_a_channel_can_override_the_question_settings() -> None:
    data = {
        "questions": {"per_hour": 5},
        "channels": {
            CH: {
                "repos": [{"repo": "acme-co/web", "nickname": "web", "description": "W"}],
                "questions": {"enabled": False},
            }
        },
    }
    settings = Policy.model_validate(data).questions_for(CH)
    assert (settings.enabled, settings.per_hour, settings.per_person_seconds) == (False, 5, 60)


# --- the answer -------------------------------------------------------------------------------

REPOS = ["acme-co/web"]


@pytest.mark.parametrize(
    ("text", "posted"),
    [
        ("It's acme-co/web#42.", "It's acme-co/web#42."),
        ("ACME-CO/WEB#42 covers it", "acme-co/web#42 covers it"),
        ("Try other/repo#1 instead", "Try instead"),
        ("See https://evil.example/x now", "See [link removed] now"),
        ("See <https://evil.example|here>", "See &lt;[link removed]|here&gt;"),
        ("Go to www.evil.io", "Go to [link removed]"),
        ("Visit evil.example/login", "Visit [link removed]"),
        ("Mail ops@acme.example", "Mail [link removed]"),
        ("exa\u200bmple.com", "[link removed]"),
        ("ex\uff0eample.com", "[link removed]"),
        ("ex%2Eample.com", "[link removed]"),
        ("mailto:ops@acme.example", "[link removed]"),
        ("Ask @octocat", "Ask octocat"),
        ("<@U0BOB> knows", "&lt;U0BOB&gt; knows"),
        ("<!channel> it's done", "it's done"),
        ("[click](https://evil.example)", "[click]([link removed]"),
        ("e.g. the CSV one.", "e.g. the CSV one."),
        ("<!<!here>channel> done", "channel&gt; done"),  # escaped text, no broadcast
        ("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444 now", "[link removed] now"),
        (
            "\u4f8b\u3048.\u30c6\u30b9\u30c8/x and m\u00fcnchen.de",
            "[link removed] and [link removed]",
        ),
        ("ops@\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444", "[link removed]"),
        ("version 1.2 and v2.0 stay", "version 1.2 and v2.0 stay"),
        ("@evil.example and @x.example/y", "[link removed] and [link removed]"),
    ],
)
def test_the_answer_holds_no_link_no_ping_and_no_other_repo(text: str, posted: str) -> None:
    assert safe_text(text, REPOS)[0] == posted


QUESTION = Grant(run_id="run-q", kind=RunKind.QUESTION, channel_id=CH, thread_ts=ROOT)


@pytest.fixture
def tools(clock: Clock, poster: Poster) -> tuple[WriteTools, LiveStore, Poster]:
    store = LiveStore(clock)
    store.register(QUESTION)
    return WriteTools(store, policy(), poster, clock=clock), store, poster


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def poster() -> Poster:
    return Poster()


async def test_an_answer_is_posted_once_as_plain_text_in_the_thread(
    tools: tuple[WriteTools, LiveStore, Poster],
) -> None:
    write, _, poster = tools
    result: Any = await write.answer(
        QUESTION, AnswerArgs(text="Yes: acme-co/web#42, see x.example")
    )
    assert result.removed == 1
    assert poster.plain == [(CH, ROOT, f"{ANSWER_PREFIX} Yes: acme-co/web#42, see [link removed]")]
    with pytest.raises(ToolError):
        await write.answer(QUESTION, AnswerArgs(text="And another thing"))
    assert len(poster.plain) == 1


async def test_an_answer_is_refused_in_a_paused_channel(
    tools: tuple[WriteTools, LiveStore, Poster],
) -> None:
    write, store, poster = tools
    await store.set_channel_mode(CH, ChannelMode.PAUSED, changed_by="U0LEAD")
    with pytest.raises(ToolError) as refused:
        await write.answer(QUESTION, AnswerArgs(text="Yes."))
    assert str(refused.value).startswith("this channel is paused")
    assert poster.plain == []


def test_an_answer_is_at_most_500_characters() -> None:
    with pytest.raises(ValueError):
        AnswerArgs(text="x" * 501)


async def test_two_askers_in_one_thread_share_one_queued_question_run(tmp_path: Any) -> None:
    from jotflow.store import SqliteStore

    for store in (MemoryStore(), await SqliteStore.open(tmp_path / "jotflow.db")):
        try:
            questions, _, _, _ = handler(store)
            await questions.on_message(event("ask: one", ts="1.1", thread_ts=ROOT, user="U0P1"))
            await questions.on_message(event("ask: two", ts="1.2", thread_ts=ROOT, user="U0P2"))
            runs = await store.thread_runs(CH, ROOT)
            assert [r.kind for r in runs] == [RunKind.QUESTION]
        finally:
            await store.close()


async def test_an_answer_may_name_a_repo_found_by_a_channels_scope(clock: Clock) -> None:
    from jotflow.models import DiscoveredRepo
    from jotflow.repo_catalog import RepoCatalog

    scoped = Policy.model_validate(
        {
            "channels": {
                CH: {
                    "repos": [{"repo": "acme-co/docs", "nickname": "docs", "description": "D"}],
                    "scopes": [{"owner": "acme-co", "match": ["web*"]}],
                }
            }
        }
    )
    found = DiscoveredRepo.model_validate(
        {"id": 1, "full_name": "acme-co/web", "pushed_at": "2026-10-19T00:00:00Z"}
    )

    async def lister(owner: str) -> list[DiscoveredRepo]:
        return [found]

    async def access(owner: str, name: str) -> bool:
        return True

    repos = RepoCatalog(scoped, lister=lister, access=access)
    await repos.refresh("acme-co")
    store, poster = LiveStore(clock), Poster()
    store.register(QUESTION)
    write = WriteTools(store, scoped, poster, clock=clock, repos=repos)
    await write.answer(QUESTION, AnswerArgs(text="See acme-co/web#7, acme-co/docs#2, other/x#1."))
    assert poster.plain == [(CH, ROOT, f"{ANSWER_PREFIX} See acme-co/web#7, acme-co/docs#2, .")]
