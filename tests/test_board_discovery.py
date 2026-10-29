"""Board discovery (#309 PR 2): board scopes find a channel's boards live.

Security's conditions: a board is in scope by number, or by a marker on a board one
of the listed people created, matched by account id. Titles never put a board in
scope. The runner filters, so an excluded board never leaves it. A found board is
marked "found by scope", capped at 20 a channel, and re-checked when it's added.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters import github_projects
from jotflow.adapters.github_projects import FIND_BOARDS, MAX_FIND_PAGES, find_boards
from jotflow.adapters.github_repos import GithubError
from jotflow.board_catalog import MAX_FOUND, BoardCatalog
from jotflow.models import (
    ExecutionResult,
    FoundBoard,
    IssueProposal,
    ProposalRecord,
    ProposalState,
    ProposedBoard,
)
from jotflow.orchestrator.boards import BoardAdder
from jotflow.orchestrator.cards import BOARDS_BLOCK, CardContext, render_card
from jotflow.policy import Policy, has_marker, parse_policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore
from tests.test_boards import FakeRunner
from tests.test_github_projects import FakeGraphql
from tests.test_github_repos import FakeGithub, runner_http
from tests.test_runner import READ
from tests.test_runs import START

CH = "C0EXAMPLE1"
THREAD = "1792310400.000100"
LEAD_ID = 1001
TOML = f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "example-org/web"
nickname = "web"
description = "The web app"

[[channels.{CH}.boards]]
board = "example-org/3"
nickname = "roadmap"
description = "Planned work for the next two quarters"

[[channels.{CH}.board_scopes]]
owner = "example-org"
numbers = [12, 14, 3]
marker = "jotflow:triage"
creators = ["example-lead"]
exclude = ["* (archive)"]
description = "Each team's own board"
aliases = {{ design = 12 }}
"""
POLICY = parse_policy(TOML)


def node(
    number: int,
    title: str = "Team board",
    description: str | None = None,
    *,
    creator: dict[str, Any] | None = None,
    closed: bool = False,
    template: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "shortDescription": description,
        "closed": closed,
        "template": template,
        "creator": creator,
    }


def person(user_id: int) -> dict[str, Any]:
    return {"__typename": "User", "databaseId": user_id}


def page(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return {"data": {"organization": {"projectsV2": {"nodes": nodes, "pageInfo": info}}}}


class Pages(FakeGraphql):
    """GitHub's GraphQL endpoint, answering the board query page by page."""

    def __init__(self, *answers: Any) -> None:
        super().__init__()
        self.pages = list(answers)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body["query"] != FIND_BOARDS:
            return super().__call__(request)
        self.seen.append(body)
        answer = self.pages.pop(0)
        return answer if isinstance(answer, httpx2.Response) else httpx2.Response(200, json=answer)


async def find(fake: Pages, **scope: Any) -> list[FoundBoard]:
    args: dict[str, Any] = {
        "numbers": frozenset({12, 14}),
        "marker": "jotflow:triage",
        "creator_ids": frozenset({LEAD_ID}),
        "exclude": ("* (archive)",),
    }
    return await find_boards("example-org", READ, http=fake.client(), **{**args, **scope})


# Policy


@pytest.mark.parametrize(
    ("scope", "problem"),
    [
        ('exclude = ["x"]', "needs numbers or a marker; titles alone never count"),
        ('marker = "jotflow:triage"', "a marker needs creators"),
        ('marker = "Triage"\ncreators = ["example-lead"]', "lowercase name:word"),
        ("numbers = [0]", "from 1 to 1,000,000,000"),
        ("numbers = [4, 4]", "listed more than once"),
        ('marker = "jotflow:x"\ncreators = ["some-bot[bot]"]', "not a person's GitHub login"),
        ('numbers = [4]\nexclude = ["bad\\u202etitle"]', "a title glob is 1 to 100 plain"),
    ],
)
def test_a_board_scope_must_name_boards_by_number_or_a_marker_and_creators(
    scope: str, problem: str
) -> None:
    base = TOML.split(f"[[channels.{CH}.board_scopes]]")[0]
    with pytest.raises(Exception, match=problem):
        parse_policy(base + f'[[channels.{CH}.board_scopes]]\nowner = "example-org"\n{scope}\n')


def test_a_board_scope_survives_a_rewrite_and_its_aliases_are_nicknames() -> None:
    assert parse_policy(render_policy(POLICY)) == POLICY
    clash = TOML.replace("aliases = { design = 12 }", "aliases = { web = 12 }")
    with pytest.raises(Exception, match="nicknames used more than once: web"):
        parse_policy(clash)


@pytest.mark.parametrize(
    ("description", "marked"),
    [
        ("Ours. jotflow:triage", True),
        ("JOTFLOW:TRIAGE first", True),
        ("jotflow:triage-old", False),
        ("not-jotflow:triage", False),
        ("jotflow:triaged", False),
        (None, False),
    ],
)
def test_the_marker_is_a_whole_word_ignoring_case(description: str | None, marked: bool) -> None:
    assert has_marker(description, "jotflow:triage") is marked


# The runner's query


async def test_the_runner_keeps_only_boards_in_scope() -> None:
    fake = Pages(
        page(
            [
                node(12, "Design"),  # by number
                node(20, "Ops", "Ours jotflow:triage", creator=person(LEAD_ID)),  # by marker
                node(21, "Theirs", "jotflow:triage", creator=person(2002)),  # another person
                node(22, "Bot", "jotflow:triage", creator={"__typename": "Bot"}),
                node(23, "Nobody", "jotflow:triage", creator=None),
                node(24, "Old", "jotflow:triage-old", creator=person(LEAD_ID)),
                node(14, "Q1 (archive)"),  # excluded by title
                node(30, "Team web"),  # a title alone never counts
                node(31, "Closed", "jotflow:triage", creator=person(LEAD_ID), closed=True),
                node(32, "Template", "jotflow:triage", creator=person(LEAD_ID), template=True),
            ]
        )
    )
    found = await find(fake)
    assert [(b.number, b.title) for b in found] == [(12, "Design"), (20, "Ops")]
    assert found[1].description == "Ours jotflow:triage"
    [sent] = fake.seen
    assert sent["query"] == FIND_BOARDS and sent["variables"] == {
        "owner": "example-org",
        "cursor": None,
    }


async def test_pages_are_followed_with_the_cursor_and_capped() -> None:
    fake = Pages(page([node(12)], cursor="Y3Vyc29yOjE="), page([node(14)]))
    assert [b.number for b in await find(fake, exclude=())] == [12, 14]
    assert fake.seen[1]["variables"] == {"owner": "example-org", "cursor": "Y3Vyc29yOjE="}
    endless = Pages(*[page([node(12)], cursor="c") for _ in range(MAX_FIND_PAGES)])
    with pytest.raises(GithubError, match="more than 500 boards"):
        await find(endless)
    with pytest.raises(GithubError, match="no cursor"):
        await find(Pages(page([node(12)], cursor="")))


@pytest.mark.parametrize(
    ("answer", "denied"),
    [
        (httpx2.Response(200, text="x" * (300 * 1024)), False),
        ({"errors": [{"type": "INTERNAL"}], "data": None}, False),
        ({"errors": [{"type": "INSUFFICIENT_SCOPES"}], "data": None}, True),
        ({"data": {"organization": None}}, False),
    ],
)
async def test_a_bad_answer_returns_nothing_partial(answer: Any, denied: bool) -> None:
    with pytest.raises(GithubError) as raised:
        await find(Pages(page([node(12)], cursor="next"), answer))
    assert isinstance(raised.value, github_projects.Denied) is denied


async def test_the_drafting_runner_serves_board_finding_with_a_strict_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    graphql = Pages(page([node(12, "Design"), node(30, "Secret plans")]))
    fake = FakeGithub({"/graphql": graphql})
    scope = {
        "owner": "example-org",
        "numbers": [12],
        "marker": None,
        "creator_ids": [],
        "exclude": [],
    }
    async with runner_http(tmp_path, fake) as http:
        answer = await http.post("/boards/find", json=scope)
        assert answer.status_code == 200
        assert answer.json() == {"boards": [{"number": 12, "title": "Design", "description": None}]}
        for bad in (
            {**scope, "extra": 1},
            {**scope, "numbers": ["12"]},
            {**scope, "numbers": [True]},
            {**scope, "creator_ids": list(range(1, 52))},
            {**scope, "marker": "Not A Marker"},
            {**scope, "owner": "example org"},
        ):
            assert (await http.post("/boards/find", json=bad)).status_code == 400
        assert (await http.post("/boards/find?owner=x", json=scope)).status_code in (400, 403)
    # An excluded board's title never leaves the runner, nor reaches its log.
    assert "Secret plans" not in answer.text and "Secret plans" not in caplog.text
    assert all(r.headers["Authorization"] == f"Bearer {READ}" for r in fake.seen)


async def test_the_execution_runner_never_finds_boards(tmp_path: Path) -> None:
    async with runner_http(tmp_path, FakeGithub({}), execution=True) as http:
        answer = await http.post("/boards/find", json={"owner": "example-org", "numbers": [3]})
        assert answer.status_code == 404


async def test_a_read_token_that_cant_list_boards_is_its_own_answer(tmp_path: Path) -> None:
    graphql = Pages({"errors": [{"type": "INSUFFICIENT_SCOPES"}], "data": None})
    async with runner_http(tmp_path, FakeGithub({"/graphql": graphql})) as http:
        answer = await http.post("/boards/find", json={"owner": "example-org", "numbers": [3]})
        assert answer.status_code == 403


async def test_the_client_sends_the_scope_as_a_body_and_checks_the_reply() -> None:
    from jotflow.orchestrator.launch import RunnerError
    from tests.test_launch import Recorder, recording_client

    recorder = Recorder(
        200, {"boards": [{"number": 12, "title": "D"}, {"number": 12, "title": "D"}]}
    )
    found = await recording_client(recorder).find_boards(
        "example-org", numbers=[12], marker=None, creator_ids=[LEAD_ID], exclude=["x*"]
    )
    assert [b.number for b in found] == [12]  # each number once
    [sent] = recorder.requests
    assert sent.method == "POST" and sent.url.path == "/boards/find" and not sent.url.query
    assert json.loads(sent.content) == {
        "owner": "example-org",
        "numbers": [12],
        "marker": None,
        "creator_ids": [LEAD_ID],
        "exclude": ["x*"],
    }
    for status, reason in ((429, "rate limit"), (403, "can't list boards")):
        with pytest.raises(RunnerError, match=reason):
            await recording_client(Recorder(status, {})).find_boards(
                "example-org", numbers=[12], marker=None, creator_ids=[], exclude=[]
            )


# The catalog


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Finder:
    def __init__(self, *boards: FoundBoard) -> None:
        self.boards = list(boards)
        self.asked: list[tuple[Any, ...]] = []
        self.fail = False

    async def __call__(self, owner: str, numbers: Any, marker: Any, ids: Any, exclude: Any) -> Any:
        self.asked.append((owner, tuple(numbers), marker, tuple(ids), tuple(exclude)))
        if self.fail:
            raise ConnectionError("runner")
        return list(self.boards)


def found(number: int, title: str = "Team board") -> FoundBoard:
    return FoundBoard(number=number, title=title, description="jotflow:triage")


def catalog(finder: Finder, clock: Clock, policy: Policy = POLICY, **kw: Any) -> BoardCatalog:
    async def resolve(login: str) -> int | None:
        return {"example-lead": LEAD_ID}.get(login)

    return BoardCatalog(policy, finder=finder, resolve=resolve, clock=clock, **kw)


async def test_found_boards_follow_listed_ones_marked_with_their_nicknames() -> None:
    finder, clock = Finder(found(12, "Design"), found(14), found(3, "Listed already")), Clock()
    boards = catalog(finder, clock)
    assert await boards.refresh(CH, 0)
    # The creators went to the runner as account ids, never logins.
    assert finder.asked == [
        ("example-org", (12, 14, 3), "jotflow:triage", (LEAD_ID,), ("* (archive)",))
    ]
    entries = boards.boards(CH)
    assert [(e.board, e.nickname, e.found) for e in entries] == [
        ("example-org/3", "roadmap", False),  # listed wins, with the team's words
        ("example-org/12", "design", True),
        ("example-org/14", "b14", True),
    ]
    assert entries[1].description == "Each team's own board" and entries[1].title == "Design"
    assert boards.board(CH, "b14") == entries[2]
    assert boards.board(CH, "EXAMPLE-ORG/12") == entries[1]


async def test_a_creator_who_isnt_a_person_drops_out_and_fails_closed() -> None:
    finder, clock = Finder(), Clock()

    async def resolve(login: str) -> int | None:
        return None  # an organisation, or no such account

    boards = BoardCatalog(POLICY, finder=finder, resolve=resolve, clock=clock)
    await boards.refresh(CH, 0)
    assert finder.asked[0][3] == ()  # no creator ids: the marker matches nothing


async def test_a_stale_listing_counts_as_none_and_a_failed_refresh_keeps_the_last() -> None:
    finder, clock = Finder(found(14)), Clock()
    boards = catalog(finder, clock)
    await boards.refresh(CH, 0)
    finder.fail = True
    assert not await boards.refresh(CH, 0)
    assert boards.board(CH, "b14") is not None  # the last good listing, still fresh
    clock.now += timedelta(hours=25)
    assert boards.board(CH, "b14") is None  # too old to trust


async def test_found_boards_are_capped_and_the_log_gives_counts_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    many = [found(100 + i, f"Secret title {i}") for i in range(MAX_FOUND + 5)]
    scope = TOML.replace("numbers = [12, 14, 3]", "numbers = [12]")
    boards = catalog(Finder(*many), Clock(), parse_policy(scope))
    await boards.refresh(CH, 0)
    entries = [e for e in boards.boards(CH) if e.found]
    assert len(entries) == MAX_FOUND and entries[0].nickname == "b100"
    assert "5 found boards past the cap of 20" in caplog.text
    assert "Secret title" not in caplog.text


async def test_a_found_board_whose_nickname_is_taken_is_left_out() -> None:
    clash = TOML.replace('nickname = "roadmap"', 'nickname = "b14"')
    boards = catalog(Finder(found(14)), Clock(), parse_policy(clash))
    await boards.refresh(CH, 0)
    assert [e.board for e in boards.boards(CH)] == ["example-org/3"]


async def test_write_access_is_asked_once_and_only_a_real_no_leaves_a_board_out() -> None:
    answers: dict[int, Any] = {12: True, 14: False}
    asked: list[int] = []

    async def access(owner: str, number: int) -> bool:
        asked.append(number)
        answer = answers[number]
        if isinstance(answer, Exception):
            raise answer
        return bool(answer)

    finder = Finder(found(12), found(14))
    boards = catalog(finder, Clock(), access=access)
    await boards.refresh(CH, 0)
    await boards.refresh(CH, 0)
    assert asked == [12, 14]  # once each, cached
    offered = [e.nickname for e in boards.offered(CH)]
    assert offered == ["roadmap", "design"]  # b14 is a real no
    b14 = boards.board(CH, "b14")
    assert b14 is not None and not boards.usable(b14)
    # A check that couldn't run keeps the board offered, and is asked again.
    finder.boards.append(found(16))
    answers[16] = ConnectionError("down")
    scope = TOML.replace("numbers = [12, 14, 3]", "numbers = [12, 14, 16]")
    retry = catalog(finder, Clock(), parse_policy(scope), access=access)
    await retry.refresh(CH, 0)
    await retry.refresh(CH, 0)
    assert asked.count(16) == 2 and "b16" in [e.nickname for e in retry.offered(CH)]


# Where found boards are used


async def loaded() -> BoardCatalog:
    boards = catalog(Finder(found(12, "Design <!here>"), found(14)), Clock())
    await boards.refresh(CH, 0)
    return boards


async def test_the_packet_marks_a_found_boards_github_text_untrusted() -> None:
    from jotflow.whiteboard.read_tools import read_tools
    from tests.test_read_tools import CH as READ_CH
    from tests.test_read_tools import FakeSource, call, running

    policy = parse_policy(TOML.replace(CH, READ_CH))
    boards = BoardCatalog(policy, finder=Finder(found(12, "Design")), clock=Clock())
    await boards.refresh(READ_CH, 0)
    store = MemoryStore()
    specs = {s.name: s for s in read_tools(store, policy, FakeSource(), boards=boards)}
    work: Any = await call(specs["get_work"], await running(store))
    found_brief = work.conversation.channel.boards[1]
    assert found_brief.discovered and found_brief.nickname == "design"
    assert found_brief.github_title.text == "Design"
    assert found_brief.description == "Each team's own board"


async def test_propose_accepts_a_found_board_only_while_it_is_in_a_fresh_listing() -> None:
    from jotflow.models import MessageRef
    from jotflow.whiteboard.tools import ToolError
    from jotflow.whiteboard.write_tools import WriteTools
    from tests.test_boards import THREAD as BOARD_THREAD
    from tests.test_write_tools import (
        DRAFT,
        Ids,
        LiveStore,
        Poster,
        issue,
        link,
    )
    from tests.test_write_tools import (
        Clock as ToolClock,
    )

    tool_clock = ToolClock()
    store = LiveStore(tool_clock)
    await store.record_packet(
        "run-1", [MessageRef(channel_id=CH, ts=BOARD_THREAD, thread_ts=BOARD_THREAD)]
    )
    clock = Clock()
    boards = catalog(Finder(found(14)), clock)
    await boards.refresh(CH, 0)
    tools = WriteTools(store, POLICY, Poster(), clock=tool_clock, new_id=Ids(), boards=boards)
    topic: Any = await tools.link_message(DRAFT, link())
    args = issue(
        topic.topic_id,
        repo="example-org/web",
        boards=[{"board": "b14", "reason": "Its team's board"}],
        sources=[{"channel_id": CH, "ts": BOARD_THREAD}],
    )
    clock.now += timedelta(hours=25)
    with pytest.raises(ToolError, match="isn't one of this channel's boards"):
        await tools.propose_issue(DRAFT, args)
    clock.now -= timedelta(hours=25)
    result: Any = await tools.propose_issue(DRAFT, args)
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and isinstance(record.proposal, IssueProposal)
    assert [b.board for b in record.proposal.boards] == ["example-org/14"]


async def test_the_card_says_found_by_scope_in_its_lines_and_menu() -> None:
    from tests.test_runs import _proposal_record

    boards = await loaded()
    record = _proposal_record("0123456789abcdef")
    draft = IssueProposal.model_validate(
        {
            **record.proposal.model_dump(),
            "boards": [{"board": "example-org/14", "reason": "Its team's board"}],
        }
    )
    record = record.model_copy(update={"proposal": draft, "channel_id": CH})
    context = CardContext(
        channel=POLICY.channel(CH),
        boards=tuple(boards.boards(CH)),
        offered_boards=tuple(boards.offered(CH)),
    )
    blocks = render_card(record, context).blocks
    text = json.dumps(blocks)
    assert "Add to boards: b14 (example-org/14), found by scope" in text
    [menu] = [b for b in blocks if str(b.get("block_id", "")).startswith(BOARDS_BLOCK)]
    labels = [o["text"]["text"] for o in menu["accessory"]["options"]]
    assert labels == [
        "roadmap: example-org/3",
        "design: example-org/12, found by scope",
        "b14: example-org/14, found by scope",
    ]


async def test_the_add_rechecks_scope_unless_the_board_was_already_added() -> None:
    finder, clock = Finder(found(14)), Clock()
    boards = catalog(finder, clock)
    await boards.refresh(CH, 0)
    record = ProposalRecord(
        id="p1",
        channel_id=CH,
        thread_ts=THREAD,
        run_id="run-1",
        state=ProposalState.APPLIED,
        proposal=IssueProposal(
            repo="example-org/web",
            title="T",
            body="B",
            confidence=0.9,
            boards=[ProposedBoard(board="example-org/14", reason="Its team's")],
        ),
        content_hash="sha256:" + "0" * 64,
        approved_by="U0CAROL",
        result=ExecutionResult(issue_url="https://github.com/example-org/web/issues/42"),
        created_at=START,
    )
    store, runner = MemoryStore(), FakeRunner()
    adder = BoardAdder(store, POLICY, runner, boards=boards)
    assert (await adder.add_all(record)).added == ["b14"]
    clock.now += timedelta(hours=25)  # the listing goes stale after the add
    # Out of the listing now, so named by owner/number, but the ledger says it's added.
    assert (await adder.add_all(record)).added == ["example-org/14"]
    fresh = BoardAdder(MemoryStore(), POLICY, runner, boards=boards)
    assert (await fresh.add_all(record)).failed == ["example-org/14"]  # never added now
    assert len(runner.added) == 1


async def test_the_repos_reply_lists_found_boards_escaped_and_marked() -> None:
    from jotflow.orchestrator.help_blocks import repos_reply

    boards = await loaded()
    _, blocks = repos_reply(POLICY, CH, "", boards=boards)
    shown = json.dumps(blocks)
    assert "Boards" in shown and "found by scope" in shown
    assert "<!here>" not in shown  # GitHub's title is escaped: nothing pings
    assert "Planned work for the next two quarters" in shown
    narrowed, _ = repos_reply(POLICY, CH, "web", boards=boards)
    assert "Boards:" not in narrowed  # a word narrows the repo list only


def test_check_says_board_scopes_need_the_read_token_too() -> None:
    from jotflow.commands.check import board_notes

    notes = board_notes(POLICY)
    assert len(notes) == 2
    assert notes[1].startswith("board scopes for example-org: the read token needs Projects")
