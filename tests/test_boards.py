"""GitHub Projects boards (#309): policy, propose, card, the add, and the start-up check."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jotflow.models import (
    ExecutionResult,
    IssueProposal,
    ProposalRecord,
    ProposalState,
    ProposedBoard,
)
from jotflow.orchestrator.boards import BoardAdder
from jotflow.orchestrator.cards import CardContext, render_card
from jotflow.orchestrator.thread_notice import ThreadNotice
from jotflow.policy import Policy, PolicyError, parse_policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore, content_hash
from jotflow.store.base import cleared_draft
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import WriteTools
from tests.test_runs import START
from tests.test_thread_notice import FakeThreadSlack
from tests.test_write_tools import DRAFT, Clock, Ids, LiveStore, Poster, issue, link

CH = "C0EXAMPLE1"
THREAD = "1792310400.000100"
TOML = f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app"
labels = ["bug", "enhancement"]

[[channels.{CH}.boards]]
board = "acme-co/3"
nickname = "roadmap"
description = "Planned work for the next two quarters"

[[channels.{CH}.boards]]
board = "acme-co/7"
nickname = "support"
description = "Customer-reported problems"
"""
POLICY = parse_policy(TOML)


# Policy


def test_boards_are_listed_with_the_teams_own_descriptions() -> None:
    channel = POLICY.channel(CH)
    roadmap = channel.board("roadmap")
    assert roadmap is not None and (roadmap.owner, roadmap.number) == ("acme-co", 3)
    assert channel.board("ACME-CO/7") is channel.board("support")
    assert channel.board("acme-co/9") is None
    # The writer keeps them on a rewrite.
    assert parse_policy(render_policy(POLICY)).channel(CH).boards == channel.boards


@pytest.mark.parametrize(
    ("board", "problem"),
    [
        ("acme-co", "owner/number"),
        ("acme-co/0", "owner/number"),
        ("acme co/3", "owner/number"),
        ("acme-co/x", "owner/number"),
        ("acme-co/1000000001", "1,000,000,000"),
    ],
)
def test_a_board_must_be_owner_and_number(board: str, problem: str) -> None:
    text = TOML.replace('board = "acme-co/3"', f'board = "{board}"')
    with pytest.raises(PolicyError, match=problem):
        parse_policy(text)


def test_nicknames_and_boards_are_unique_in_a_channel() -> None:
    with pytest.raises(PolicyError, match="nicknames used more than once: web"):
        parse_policy(TOML.replace('nickname = "roadmap"', 'nickname = "web"'))
    with pytest.raises(PolicyError, match="boards listed more than once"):
        parse_policy(TOML.replace('board = "acme-co/7"', 'board = "acme-co/3"'))
    with pytest.raises(PolicyError, match="description"):
        parse_policy(TOML.replace("Planned work for the next two quarters", "Planned\\u0007work"))


# Propose and the packet


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> LiveStore:
    return LiveStore(clock)


async def tools_for(store: LiveStore, clock: Clock, off: set[str] | None = None) -> WriteTools:
    from jotflow.models import MessageRef

    await store.record_packet("run-1", [MessageRef(channel_id=CH, ts=THREAD, thread_ts=THREAD)])
    return WriteTools(store, POLICY, Poster(), clock=clock, new_id=Ids(), boards_off=off or set())


async def test_propose_keeps_only_the_channels_boards_with_cleaned_reasons(
    store: LiveStore, clock: Clock
) -> None:
    tools = await tools_for(store, clock)
    topic: Any = await tools.link_message(DRAFT, link())
    boards = [
        {"board": "roadmap", "reason": "Planned work <!here> for Q3"},
        {"board": "ACME-CO/3", "reason": "The same board again"},
        {"board": "acme-co/7", "reason": "A customer reported it"},
    ]
    result: Any = await tools.propose_issue(
        DRAFT, issue(topic.topic_id, boards=boards, sources=[{"channel_id": CH, "ts": THREAD}])
    )
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and isinstance(record.proposal, IssueProposal)
    assert [(b.board, b.reason) for b in record.proposal.boards] == [
        ("acme-co/3", "Planned work for Q3"),
        ("acme-co/7", "A customer reported it"),
    ]


@pytest.mark.parametrize(
    ("board", "off", "problem"),
    [
        ("acme-co/9", set(), "isn't one of this channel's boards"),
        ("roadmap", {"acme-co/3"}, "is off"),
    ],
)
async def test_an_unlisted_or_off_board_is_refused(
    store: LiveStore, clock: Clock, board: str, off: set[str], problem: str
) -> None:
    tools = await tools_for(store, clock, off)
    topic: Any = await tools.link_message(DRAFT, link())
    with pytest.raises(ToolError, match=problem):
        await tools.propose_issue(
            DRAFT,
            issue(
                topic.topic_id,
                boards=[{"board": board, "reason": "Because"}],
                sources=[{"channel_id": CH, "ts": THREAD}],
            ),
        )


async def test_the_packet_lists_boards_that_are_on() -> None:
    from tests.test_read_tools import CH as READ_CH
    from tests.test_read_tools import FakeSource, call, running

    store, clock = MemoryStore(), Clock()
    grant = await running(store)
    policy = parse_policy(TOML.replace(CH, READ_CH))
    specs = {
        s.name: s
        for s in read_tools(store, policy, FakeSource(), clock=clock, boards_off={"acme-co/7"})
    }
    work: Any = await call(specs["get_work"], grant)
    boards = work.conversation.channel.boards
    assert [(b.nickname, b.board, b.description) for b in boards] == [
        ("roadmap", "acme-co/3", "Planned work for the next two quarters")
    ]


# Hash, card, and clearing


def draft(**fields: Any) -> IssueProposal:
    return IssueProposal(
        repo="acme-co/web", title="Export as CSV", body="People want it.", confidence=0.9, **fields
    )


def test_no_boards_keeps_an_older_drafts_hash() -> None:
    plain = draft()
    assert "boards" not in json.dumps(plain.model_dump()) or plain.boards == []
    from jotflow.store.hashing import canonical_json

    assert '"boards"' not in canonical_json(plain, hashed=True)
    with_boards = draft(boards=[ProposedBoard(board="acme-co/3", reason="Planned")])
    assert content_hash(with_boards) != content_hash(plain)  # boards are locked


def test_the_card_names_boards_by_nickname_and_shows_the_agents_reason() -> None:
    record = ProposalRecord(
        id="p1",
        channel_id=CH,
        thread_ts=THREAD,
        run_id="run-1",
        proposal=draft(boards=[ProposedBoard(board="acme-co/3", reason="Planned *work*")]),
        created_at=START,
    )
    card = render_card(record, CardContext(channel=POLICY.channel(CH)))
    text = json.dumps(card.blocks)
    assert "Add to boards: roadmap (acme-co/3)" in text
    assert "The agent's reason for roadmap (acme-co/3): Planned *work*" in text
    reason = next(b for b in card.blocks if "The agent's reason" in json.dumps(b))
    assert reason["elements"][0]["type"] == "plain_text"  # never formatted


def test_clearing_a_draft_keeps_its_boards_but_not_the_reasons() -> None:
    cleared = cleared_draft(draft(boards=[ProposedBoard(board="acme-co/3", reason="Per Alice")]))
    assert isinstance(cleared, IssueProposal)
    assert [(b.board, b.reason) for b in cleared.boards] == [("acme-co/3", "(cleared)")]


# The add


class FakeRunner:
    def __init__(self) -> None:
        self.added: list[tuple[str, int, str]] = []
        self.fail: set[str] = set()

    async def add_to_board(self, repo: str, issue_number: int, board: str) -> None:
        if board in self.fail:
            raise ConnectionError("runner")
        self.added.append((repo, issue_number, board))


def applied(url: str = "https://github.com/acme-co/web/issues/42", **fields: Any) -> ProposalRecord:
    boards = [
        ProposedBoard(board="acme-co/3", reason="Planned"),
        ProposedBoard(board="acme-co/7", reason="Reported"),
    ]
    return ProposalRecord(
        id="p1",
        channel_id=CH,
        thread_ts=THREAD,
        run_id="run-1",
        state=ProposalState.APPLIED,
        proposal=draft(boards=boards),
        content_hash="sha256:" + "0" * 64,
        approved_by="U0CAROL",
        result=ExecutionResult(issue_url=url),
        created_at=START,
        **fields,
    )


async def test_an_applied_issue_is_added_to_each_approved_board_once() -> None:
    store, runner = MemoryStore(), FakeRunner()
    adder = BoardAdder(store, POLICY, runner)
    results = await adder.add_all(applied())
    assert runner.added == [("acme-co/web", 42, "acme-co/3"), ("acme-co/web", 42, "acme-co/7")]
    assert results.notice() == "Added to roadmap, support."
    again = await adder.add_all(applied())  # a restart, or the notice tried again
    assert runner.added == [("acme-co/web", 42, "acme-co/3"), ("acme-co/web", 42, "acme-co/7")]
    assert again.added == ["roadmap", "support"]


async def test_a_failing_board_is_retried_then_named_for_a_person() -> None:
    store, runner = MemoryStore(), FakeRunner()
    runner.fail = {"acme-co/7"}
    adder = BoardAdder(store, POLICY, runner)
    first = await adder.add_all(applied())
    assert first.retry and first.added == ["roadmap"]
    await adder.add_all(applied())
    last = await adder.add_all(applied())
    assert not last.retry and last.failed == ["support"]
    assert last.notice() == "Added to roadmap. Couldn't add it to support; add it by hand."


async def test_a_board_no_longer_listed_or_another_repos_issue_is_never_added() -> None:
    store, runner = MemoryStore(), FakeRunner()
    narrowed = parse_policy(TOML.split(f'[[channels.{CH}.boards]]\nboard = "acme-co/7"')[0])
    results = await BoardAdder(store, narrowed, runner).add_all(applied())
    assert runner.added == [("acme-co/web", 42, "acme-co/3")]
    assert results.failed == ["acme-co/7"]  # its nickname is gone with it
    elsewhere = applied("https://github.com/acme-co/api/issues/42")
    results = await BoardAdder(MemoryStore(), POLICY, runner).add_all(elsewhere)
    assert results.failed == ["roadmap", "support"] and len(runner.added) == 1


async def test_the_thread_notice_says_where_the_issue_went() -> None:
    store, runner, slack = MemoryStore(), FakeRunner(), FakeThreadSlack()
    record = applied()
    await store.add_proposal(
        record.model_copy(
            update={
                "state": ProposalState.PENDING,
                "result": None,
                "approved_by": None,
                "content_hash": None,
            }
        )
    )
    await store.transition(
        "p1",
        ProposalState.APPROVED,
        approved_by="U0CAROL",
        expected_hash=content_hash(record.proposal),
    )
    await store.transition("p1", ProposalState.EXECUTING)
    await store.transition("p1", ProposalState.APPLIED, result=record.result)
    notice = ThreadNotice(store, POLICY, slack, boards=BoardAdder(store, POLICY, runner))
    assert await notice.tell() == 1
    [(_, _, text)] = slack.replies
    assert text.endswith("Added to roadmap, support.")


# The start-up check


async def test_boards_the_write_token_cant_update_are_turned_off() -> None:
    from jotflow.models import RunKind
    from jotflow.orchestrator.launch import RunnerError
    from jotflow.orchestrator.service import Orchestrator

    class Runner:
        async def board_access(self, owner: str, number: int) -> bool:
            if number == 7:
                raise RunnerError("down")
            return number == 3

    service = Orchestrator.__new__(Orchestrator)
    service._policy = POLICY
    service.boards_off = set()
    service._runner = Runner()  # type: ignore[assignment]
    service.kinds = type("K", (), {"on": frozenset({RunKind.EXECUTION})})()  # type: ignore[assignment]
    await service._check_boards()
    assert service.boards_off == {"acme-co/7"}
    service.boards_off = set()
    service.kinds = type("K", (), {"on": frozenset()})()  # type: ignore[assignment]
    await service._check_boards()
    assert service.boards_off == {"acme-co/3", "acme-co/7"}  # nothing could add to them


def test_policy_without_boards_is_unchanged() -> None:
    plain = Policy.model_validate(
        {
            "channels": {
                CH: {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "W"}]}
            }
        }
    )
    assert plain.channel(CH).boards == ()
