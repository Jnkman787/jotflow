"""#345 review: the Slack reads that find a change request's author must ask Slack for
message metadata. Without include_all_metadata, Slack leaves the metadata out, the
note reads as JotFlow's own post, and its writer could approve their own change."""

from __future__ import annotations

from typing import Any

from jotflow.change_requests import change_request_metadata
from jotflow.models import MessageRef
from jotflow.orchestrator.packet import BoltSlackReader
from jotflow.orchestrator.people import SlackPeople

BOT = "U0JOTBOT"
CAROL = "U0CAROL"
CH = "C0EXAMPLE1"
ROOT = "1792310400.000100"
NOTE = "1792310500.000100"


class Response(dict):  # type: ignore[type-arg]
    @property
    def data(self) -> dict[str, Any]:
        return dict(self)


class SlackLikeClient:
    """Answers as Slack does: message metadata only when include_all_metadata is true."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def auth_test(self) -> Response:
        return Response(user_id=BOT, team_id="T0ACME")

    async def conversations_replies(self, **kw: Any) -> Response:
        self.calls.append(kw)
        note: dict[str, Any] = {"ts": NOTE, "user": BOT, "text": "Change request: totals row"}
        if kw.get("include_all_metadata") is True:
            note["metadata"] = change_request_metadata(CAROL)
        return Response(messages=[note], has_more=False)


async def test_the_approval_author_lookup_asks_for_metadata() -> None:
    client = SlackLikeClient()
    people = SlackPeople(client)
    authors = await people.authors([MessageRef(channel_id=CH, ts=NOTE, thread_ts=ROOT)])
    # The note is Carol's: without the flag it would read as no one's.
    assert authors == {CAROL}
    assert all(call.get("include_all_metadata") is True for call in client.calls)


async def test_the_drafting_packet_read_asks_for_metadata() -> None:
    client = SlackLikeClient()
    reader = BoltSlackReader.__new__(BoltSlackReader)
    reader._client = client  # the copy BoltSlackReader makes, without a network client
    page = await reader.replies(CH, ROOT, cursor=None, limit=50)
    [note] = page["messages"]
    assert note["metadata"]["event_payload"]["by"] == CAROL
    assert client.calls[0]["include_all_metadata"] is True


async def test_a_change_requests_writer_cant_approve_the_card_it_revised() -> None:
    # #345 review, end to end: under require_second_approver, Dave's only message is his
    # Request Changes note, posted by JotFlow. The revised card is still his own draft.
    from jotflow.models import IssueProposal
    from jotflow.orchestrator.approval import OWN_PROPOSAL
    from tests.test_approval import BOB, DAVE, Rig, click
    from tests.test_runs import CH as RCH
    from tests.test_runs import THREAD

    note_ts = "1760781650.000100"

    class Thread:
        """Slack's replies for the card's thread: Alice's root, and JotFlow's post of
        Dave's change request, whose metadata Slack gives only when asked."""

        async def auth_test(self) -> Response:
            return Response(user_id=BOT, team_id="T0OURS")

        async def conversations_replies(self, **kw: Any) -> Response:
            ts = kw["oldest"]
            if ts == note_ts:
                note: dict[str, Any] = {"ts": ts, "user": BOT, "text": "Change request"}
                if kw.get("include_all_metadata") is True:
                    note["metadata"] = change_request_metadata(DAVE)
                return Response(messages=[note])
            return Response(messages=[{"ts": ts, "user": "U0ALICE", "text": "The idea"}])

    rig = Rig(approvers=[DAVE, BOB], second=True)
    people = SlackPeople(Thread())
    rig.slack.authors = people.authors  # type: ignore[method-assign]
    pid = await rig.proposal()
    record = await rig.store.get_proposal(pid)
    assert record is not None and isinstance(record.proposal, IssueProposal)
    revised = record.proposal.model_copy(
        update={
            "body": record.proposal.body + " With totals.",
            "sources": [MessageRef(channel_id=RCH, ts=note_ts, thread_ts=THREAD)],
        }
    )
    await rig.store.revise_proposal(
        pid, revised, run_id="run-2", base_revision=record.revision, added_by=[DAVE]
    )
    value = f"{pid}:{await rig.hash(pid)}"
    assert await rig.handler.on_approve(click(value, user=DAVE)) == "refused"
    assert rig.slack.ephemerals[-1][2] == OWN_PROPOSAL
    assert await rig.handler.on_approve(click(value, user=BOB)) == "approved"
