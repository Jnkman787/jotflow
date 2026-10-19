"""The issue footer's people and links, from Slack, never failing the write (#31)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from jotflow.execution import attribution as module
from jotflow.execution.attribution import execution_footer
from jotflow.execution.writes import marker
from jotflow.models import IssueProposal, MessageRef, ProposalRecord, ProposalState
from jotflow.orchestrator.people import SlackAttribution, SlackPeople
from jotflow.policy import parse_policy

CH = "C0EXAMPLE1"
ROOT = "1792310400.000100"
REPLY = "1792310640.000100"
POLICY = """
{links}
[channels.C0EXAMPLE1]

[[channels.C0EXAMPLE1.repos]]
repo = "acme-co/web"
nickname = "web"
description = "Web"
"""


def policy(links: str = "") -> Any:
    return parse_policy(POLICY.format(links=links))


def record(*sources: MessageRef) -> ProposalRecord:
    return ProposalRecord(
        id="p1",
        channel_id=CH,
        thread_ts=ROOT,
        run_id="r1",
        state=ProposalState.APPROVED,
        proposal=IssueProposal(
            repo="acme-co/web", title="t", body="b", confidence=0.9, sources=list(sources)
        ),
        participants=["U0ALEX", "U0BOB", "U0SAM"],
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )


class Fake:
    """Slack, as the fake knows it: who wrote each message, and their names."""

    def __init__(self, authors: dict[str, set[str]] | Exception | None = None) -> None:
        self._authors = authors if authors is not None else {ROOT: {"U0SAM"}, REPLY: {"U0ALEX"}}
        self.url: str | None = "https://acme-co.slack.com/"

    async def authors(self, refs: Any) -> set[str]:
        if isinstance(self._authors, Exception):
            raise self._authors
        found: set[str] = set()
        for ref in refs:
            if ref.ts not in self._authors:
                raise LookupError(f"message {ref.ts} was not found")
            found |= self._authors[ref.ts]
        return found

    async def name(self, user_id: str) -> str | None:
        return {"U0SAM": "Sam", "U0ALEX": "Alex @here", "U0BOB": "Bob"}.get(user_id)

    async def workspace_url(self) -> str | None:
        return self.url


SOURCES = (
    MessageRef(channel_id=CH, ts=ROOT),
    MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT),
)


async def test_the_footer_names_the_poster_and_links_each_source() -> None:
    tail = await execution_footer(record(*SOURCES), policy(), Fake())
    assert tail.splitlines() == [
        "Raised in Slack by Sam, with Alex here looped in.",
        "Slack: https://acme-co.slack.com/archives/C0EXAMPLE1/p1792310400000100, "
        "https://acme-co.slack.com/archives/C0EXAMPLE1/p1792310640000100"
        "?thread_ts=1792310400.000100&cid=C0EXAMPLE1",
        marker("p1"),
    ]


async def test_a_bot_root_has_no_poster_and_no_url_means_no_links() -> None:
    fake = Fake(authors={ROOT: set(), REPLY: {"U0ALEX"}})
    fake.url = None
    tail = await execution_footer(record(*SOURCES), policy(), fake)
    assert tail.splitlines() == ["Raised in Slack, with Alex here looped in.", marker("p1")]


async def test_looped_in_names_only_confirmed_source_authors() -> None:
    # The agent listed U0BOB as a participant, but Bob wrote none of the cited
    # sources, so his name never reaches GitHub. A source Slack cannot find adds no one.
    gone = MessageRef(channel_id=CH, ts="1792310999.000100", thread_ts=ROOT)
    tail = await execution_footer(record(*SOURCES, gone), policy(), Fake())
    assert "Bob" not in tail
    assert tail.splitlines()[0] == "Raised in Slack by Sam, with Alex here looped in."
    only_root = await execution_footer(record(SOURCES[0]), policy(), Fake())
    assert only_root.splitlines()[0] == "Raised in Slack by Sam."


async def test_a_failed_lookup_leaves_people_out_and_still_marks() -> None:
    tail = await execution_footer(record(*SOURCES), policy(), Fake(LookupError("gone")))
    assert tail.splitlines() == ["Raised in Slack.", marker("p1")]


async def test_a_slow_lookup_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    class Slow(Fake):
        async def authors(self, refs: Any) -> set[str]:
            await anyio.sleep_forever()
            raise AssertionError

    monkeypatch.setattr(module, "ATTRIBUTION_SECONDS", 0.05)
    tail = await execution_footer(record(*SOURCES), policy(), Slow())
    assert tail.splitlines() == ["Raised in Slack.", marker("p1")]


async def test_issue_links_none_is_the_marker_alone() -> None:
    tail = await execution_footer(record(*SOURCES), policy('issue_links = "none"'), Fake())
    assert tail == marker("p1")


async def test_a_source_s_own_permalink_is_used_when_valid() -> None:
    own = MessageRef(
        channel_id=CH,
        ts=ROOT,
        permalink="https://acme-co.slack.com/archives/C0EXAMPLE1/p1792310400000100",
    )
    bad = MessageRef(channel_id=CH, ts=REPLY, permalink="https://evil.example/x")
    tail = await execution_footer(record(own, bad), policy(), None)
    assert tail.splitlines() == [
        "Raised in Slack.",
        "Slack: https://acme-co.slack.com/archives/C0EXAMPLE1/p1792310400000100",
        marker("p1"),
    ]


async def test_slack_attribution_shares_one_people() -> None:
    class Client:
        calls = 0

        async def auth_test(self) -> dict[str, Any]:
            Client.calls += 1
            return {"team_id": "T0ACME", "url": "https://acme-co.slack.com/"}

        async def conversations_replies(self, **kw: Any) -> dict[str, Any]:
            return {"messages": [{"ts": kw["latest"], "user": "U0SAM"}]}

    async def name(user_id: str) -> str | None:
        return "Sam"

    people = SlackPeople(Client())
    slack = SlackAttribution(people, name)
    assert await slack.authors([MessageRef(channel_id=CH, ts=ROOT)]) == {"U0SAM"}
    assert await slack.name("U0SAM") == "Sam"
    assert await slack.workspace_url() == "https://acme-co.slack.com/"
    assert await people.own_team() == "T0ACME"
    assert Client.calls == 1
