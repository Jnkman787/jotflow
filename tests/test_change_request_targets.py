"""A change request names the card it was made on, and revises only that one (#375)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jotflow.change_requests import (
    PACKET_CHANGE_REQUESTS,
    change_request_by,
    change_request_for,
    change_request_metadata,
)
from jotflow.models import MessageRef
from jotflow.store.base import ToolCallRecord
from jotflow.whiteboard.read_tools import NoArguments
from jotflow.whiteboard.tools import ToolError
from jotflow.whiteboard.write_tools import RevisedIssue, ReviseProposalArgs, WriteTools
from tests.test_write_tools import (
    CH,
    DRAFT,
    POLICY,
    REPLY,
    ROOT,
    Clock,
    Ids,
    LiveStore,
    Poster,
    issue,
    new_topic,
)

BOT = "U0JOTBOT"
ELSEWHERE_CARD = "ffffffffffffffff"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> LiveStore:
    return LiveStore(clock)


@pytest.fixture
async def tools(store: Any, clock: Clock) -> WriteTools:
    packet = [
        MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT),
        MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT),
    ]
    await store.record_packet("run-1", packet)
    return WriteTools(store, POLICY, Poster(), clock=clock, new_id=Ids())


CARD = "0123456789abcdef"


def test_the_metadata_names_the_card_and_older_posts_name_none() -> None:
    post = {"user": BOT, "metadata": change_request_metadata("U0ALICE", CARD, "t1")}
    assert change_request_by(post, BOT) == "U0ALICE"
    assert change_request_for(post, BOT) == CARD
    older = {"user": BOT, "metadata": change_request_metadata("U0ALICE")}
    assert change_request_for(older, BOT) is None
    assert change_request_for({**post, "user": "U0OTHER"}, BOT) is None  # not JotFlow's
    odd = {"user": BOT, "metadata": change_request_metadata("U0ALICE", "not-an-id")}
    assert change_request_for(odd, BOT) is None


async def pending(tools: Any) -> str:
    made: Any = await tools.propose_issue(DRAFT, issue(await new_topic(tools)))
    return str(made.proposal_id)


async def made_on(store: Any, clock: Any, proposal_id: str) -> None:
    await store.record_tool_call(
        ToolCallRecord(
            run_id="run-1",
            tool=PACKET_CHANGE_REQUESTS,
            args_hash="change_requests",
            channel_id=CH,
            thread_ts=ROOT,
            result_json=json.dumps({REPLY: proposal_id}),
            called_at=clock(),
        )
    )


def revise(proposal_id: str, revision: int) -> ReviseProposalArgs:
    return ReviseProposalArgs(
        proposal_id=proposal_id,
        base_revision=revision,
        issue=RevisedIssue(title="Export the dashboard table as CSV", body="Also PDF."),
        sources=[MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT)],
        confidence=0.9,
    )


async def test_a_change_request_revises_only_its_own_card(
    tools: Any, store: Any, clock: Any
) -> None:
    card = await pending(tools)
    await made_on(store, clock, ELSEWHERE_CARD)  # made on another card in the thread
    current = await store.get_proposal(card)
    with pytest.raises(ToolError, match=f"is a change request for proposal {ELSEWHERE_CARD}"):
        await tools.revise_proposal(DRAFT, revise(card, current.revision))


async def test_a_change_request_on_this_card_revises_it(tools: Any, store: Any, clock: Any) -> None:
    card = await pending(tools)
    await made_on(store, clock, card)
    current = await store.get_proposal(card)
    result: Any = await tools.revise_proposal(DRAFT, revise(card, current.revision))
    assert result.revised


async def test_a_change_request_naming_no_card_revises_as_before(tools: Any, store: Any) -> None:
    card = await pending(tools)
    current = await store.get_proposal(card)
    result: Any = await tools.revise_proposal(DRAFT, revise(card, current.revision))
    assert result.revised


async def test_a_change_request_seen_by_a_later_get_work_is_checked_too(clock: Any) -> None:
    """A second get_work in the run that serves a new change request records it too,
    so the revise check knows its card (CodeRabbit on #390)."""
    from jotflow.change_requests import PACKET_CHANGE_REQUESTS
    from jotflow.store import MemoryStore
    from tests.test_read_tools import FakeSource, running
    from tests.test_read_tools import tools as read_tools_for

    store = MemoryStore(clock=clock)
    served: dict[str, str] = {"1760781700.000100": "aaaaaaaaaaaaaaaa"}

    class Requests(FakeSource):
        async def thread(self, channel_id: str, thread_ts: str) -> Any:
            work = await super().thread(channel_id, thread_ts)
            asked = [
                work.messages[0].model_copy(update={"ts": ts, "change_request_for": card})
                for ts, card in served.items()
            ]
            return work.model_copy(update={"messages": [*work.messages, *asked]})

    grant = await running(store)
    get_work = read_tools_for(store, clock, Requests())["get_work"]
    await get_work.handler(grant, NoArguments())
    served["1760781800.000100"] = "bbbbbbbbbbbbbbbb"
    await get_work.handler(grant, NoArguments())
    rows = await store.tool_call_results(grant.run_id, (PACKET_CHANGE_REQUESTS,))
    made_on: dict[str, str] = {}
    for row in rows:
        made_on |= json.loads(row)
    assert made_on == {
        "1760781700.000100": "aaaaaaaaaaaaaaaa",
        "1760781800.000100": "bbbbbbbbbbbbbbbb",
    }
