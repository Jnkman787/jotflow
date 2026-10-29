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
        "Raised in Slack by Sam, with contributions from Alex here.",
        "Slack: https://acme-co.slack.com/archives/C0EXAMPLE1/p1792310400000100, "
        "https://acme-co.slack.com/archives/C0EXAMPLE1/p1792310640000100"
        "?thread_ts=1792310400.000100&cid=C0EXAMPLE1",
        marker("p1"),
    ]


async def test_a_bot_root_has_no_poster_and_no_url_means_no_links() -> None:
    fake = Fake(authors={ROOT: set(), REPLY: {"U0ALEX"}})
    fake.url = None
    tail = await execution_footer(record(*SOURCES), policy(), fake)
    first = "Raised in Slack, with contributions from Alex here."
    assert tail.splitlines() == [first, marker("p1")]


async def test_looped_in_names_only_confirmed_source_authors() -> None:
    # The agent listed U0BOB as a participant, but Bob wrote none of the cited
    # sources, so his name never reaches GitHub. A source Slack cannot find adds no one.
    gone = MessageRef(channel_id=CH, ts="1792310999.000100", thread_ts=ROOT)
    tail = await execution_footer(record(*SOURCES, gone), policy(), Fake())
    assert "Bob" not in tail
    assert tail.splitlines()[0] == "Raised in Slack by Sam, with contributions from Alex here."
    only_root = await execution_footer(record(SOURCES[0]), policy(), Fake())
    assert only_root.splitlines()[0] == "Raised in Slack by Sam."


async def test_a_failed_lookup_leaves_people_out_and_still_marks() -> None:
    tail = await execution_footer(record(*SOURCES), policy(), Fake(LookupError("gone")))
    first, links, last = tail.splitlines()
    assert (first, last) == ("Raised in Slack.", marker("p1"))
    # The links don't depend on the people lookups (#307 review).
    assert links.startswith("Slack: https://acme-co.slack.com/archives/")


async def test_a_slow_lookup_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    class Slow(Fake):
        async def authors(self, refs: Any) -> set[str]:
            await anyio.sleep_forever()
            raise AssertionError

    monkeypatch.setattr(module, "ATTRIBUTION_SECONDS", 0.05)
    tail = await execution_footer(record(*SOURCES), policy(), Slow())
    first, links, last = tail.splitlines()
    assert (first, last) == ("Raised in Slack.", marker("p1"))
    assert links.startswith("Slack: ")  # a slow people lookup no longer costs the links


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


# Everyone who contributed (#307)

KIM = "1792310700.000100"  # Kim adds to the idea, in the same thread
OTHER_THREAD = "1792399999.000100"  # a message in another thread, linked to the same topic
CHATTER = "1792310800.000100"


class Crowd(Fake):
    def __init__(self) -> None:
        super().__init__(
            {
                ROOT: {"U0SAM"},
                REPLY: {"U0ALEX"},
                KIM: {"U0KIM"},
                OTHER_THREAD: {"U0OUTSIDE"},
                CHATTER: {"U0CHAT"},
            }
        )

    async def name(self, user_id: str) -> str | None:
        names = {"U0KIM": "@kim <!here>", "U0OUTSIDE": "Outsider", "U0CHAT": "Chatty"}
        return names.get(user_id) or await super().name(user_id)


async def topic_store() -> tuple[Any, str]:
    from jotflow.store import MemoryStore

    store = MemoryStore()
    topic = (await store.create_topic(CH, "Progress bar")).topic
    for ts, thread in ((ROOT, None), (REPLY, ROOT), (KIM, ROOT), (OTHER_THREAD, None)):
        await store.link_message(MessageRef(channel_id=CH, ts=ts, thread_ts=thread), topic.id)
    await store.mark_chatter(MessageRef(channel_id=CH, ts=CHATTER, thread_ts=ROOT))
    return store, topic.id


async def test_a_new_issue_credits_everyone_who_added_to_its_topic_in_the_thread() -> None:
    store, topic_id = await topic_store()
    draft = record(MessageRef(channel_id=CH, ts=ROOT))
    draft = draft.model_copy(
        update={"proposal": draft.proposal.model_copy(update={"topic_id": topic_id})}
    )
    tail = await execution_footer(draft, policy(), Crowd(), store)
    first = tail.splitlines()[0]
    # Sam raised it, Alex and Kim added to it in the thread; Sam isn't named twice.
    assert first == "Raised in Slack by Sam, with contributions from Alex here and kim here."
    # Never a message from another thread, nor chatter, and never a ping.
    assert "Outsider" not in tail and "Chatty" not in tail
    assert "@" not in first and "<!" not in first


async def test_a_topic_found_from_a_source_counts_too() -> None:
    store, _ = await topic_store()
    draft = record(MessageRef(channel_id=CH, ts=ROOT))  # no topic_id: a new topic
    tail = await execution_footer(draft, policy(), Crowd(), store)
    assert "contributions from Alex here and kim here." in tail.splitlines()[0]


async def test_an_update_credits_who_added_the_new_information() -> None:
    from jotflow.models import IssueChanges, UpdateProposal

    update = record().model_copy(
        update={
            "proposal": UpdateProposal(
                topic_id="t1",
                repo="acme-co/web",
                issue_number=42,
                changes=IssueChanges(comment="More detail."),
                sources=[MessageRef(channel_id=CH, ts=KIM, thread_ts=ROOT)],
                confidence=0.9,
            )
        }
    )
    tail = await execution_footer(update, policy(), Crowd())
    assert tail.splitlines()[0] == "Updated from Slack by kim here."
    nobody = await execution_footer(update, policy(), Fake({}))
    assert nobody.splitlines()[0] == "Updated from Slack."


async def test_no_names_at_all_with_issue_links_none() -> None:
    store, _ = await topic_store()
    tail = await execution_footer(record(), policy('issue_links = "none"'), Crowd(), store)
    assert tail == marker("p1")


async def test_a_draft_for_one_topic_never_credits_another_topics_people() -> None:
    store, topic_a = await topic_store()
    topic_b = (await store.create_topic(CH, "Dark mode")).topic.id
    cited, other = "1792310900.000100", "1792310950.000100"
    for ts in (cited, other):
        await store.link_message(MessageRef(channel_id=CH, ts=ts, thread_ts=ROOT), topic_b)
    crowd = Crowd()
    crowd._authors[cited] = {"U0SAM"}  # type: ignore[index]
    crowd._authors[other] = {"U0OUTSIDE"}  # type: ignore[index]
    # Topic A's draft cites one of topic B's messages: B's other people stay out.
    draft = record(
        MessageRef(channel_id=CH, ts=ROOT), MessageRef(channel_id=CH, ts=cited, thread_ts=ROOT)
    )
    draft = draft.model_copy(
        update={"proposal": draft.proposal.model_copy(update={"topic_id": topic_a})}
    )
    tail = await execution_footer(draft, policy(), crowd, store)
    assert "Outsider" not in tail
    assert "contributions from Alex here and kim here." in tail.splitlines()[0]


# The card names exactly who the footer will credit (#307 review)


class Names:
    async def name(self, user_id: str) -> str | None:
        return None


async def card_line(draft: ProposalRecord, store: Any, links: str = "") -> str | None:
    from jotflow.orchestrator.card_sync import card_context

    context = await card_context(policy(links), store, Names(), draft, attribution=Crowd())
    return context.credit_line


def on_topic(draft: ProposalRecord, topic_id: str) -> ProposalRecord:
    return draft.model_copy(
        update={"proposal": draft.proposal.model_copy(update={"topic_id": topic_id})}
    )


async def test_the_card_shows_the_names_the_footer_will_carry() -> None:
    store, topic_id = await topic_store()
    draft = on_topic(record(MessageRef(channel_id=CH, ts=ROOT)), topic_id)
    assert await card_line(draft, store) == (
        "Credited in the issue: raised by Sam, with contributions from Alex here and kim here"
    )
    tail = await execution_footer(draft, policy(), Crowd(), store)
    assert tail.splitlines()[0] == (
        "Raised in Slack by Sam, with contributions from Alex here and kim here."
    )
    # No names go to GitHub, so the card names nobody.
    assert await card_line(draft, store, 'issue_links = "none"') is None


async def test_a_reply_after_the_draft_is_credited_neither_on_the_card_nor_the_issue() -> None:
    store, topic_id = await topic_store()
    late = "1792399000.000100"  # after the draft was made, below
    await store.link_message(MessageRef(channel_id=CH, ts=late, thread_ts=ROOT), topic_id)
    crowd = Crowd()
    crowd._authors[late] = {"U0OUTSIDE"}  # type: ignore[index]
    draft = on_topic(record(MessageRef(channel_id=CH, ts=ROOT)), topic_id).model_copy(
        update={"created_at": datetime.fromtimestamp(1792320000, tz=UTC)}
    )
    tail = await execution_footer(draft, policy(), crowd, store)
    assert "Outsider" not in tail
    from jotflow.orchestrator.card_sync import card_context

    context = await card_context(policy(), store, Names(), draft, attribution=crowd)
    assert context.credit_line is not None and "Outsider" not in context.credit_line


async def test_the_card_says_when_it_could_not_check(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    from jotflow.orchestrator import card_sync

    class Slow(Crowd):
        async def authors(self, refs: Any) -> set[str]:
            await anyio.sleep_forever()
            raise AssertionError

    monkeypatch.setattr(card_sync, "CREDIT_SECONDS", 0.05)
    store, _ = await topic_store()
    context = await card_sync.card_context(
        policy(), store, Names(), record(*SOURCES), attribution=Slow()
    )
    assert context.credit_line == card_sync.CREDIT_UNKNOWN


def test_the_credit_line_for_an_update_names_only_who_added() -> None:
    from jotflow.execution.attribution import Credit

    assert Credit(others=("Kim",)).line(update=True) == "Credited in the update: Kim"
    assert Credit().line(update=True) is None
    assert Credit().line(update=False) is None
    assert Credit(poster="@Sam").line(update=False) == "Credited in the issue: raised by Sam"


def test_the_card_line_uses_the_footers_own_rules() -> None:
    from jotflow.execution.attribution import Credit
    from jotflow.execution.writes import footer

    crowd = Credit(poster="Sam", others=("sam", "Kim", "kim", *(f"N{i}" for i in range(12))))
    line = crowd.line(update=False)
    filed = footer("p1", poster=crowd.poster, others=list(crowd.others)).splitlines()[0]
    # The same names, once each, the poster not repeated, and the same cap.
    assert line is not None and line.endswith("N7 and 4 others")
    assert filed.endswith("N7 and 4 others.")
    assert line.count("Kim") + line.count("kim") == 1 and "sam" not in line


async def test_a_revision_s_new_author_is_credited_on_the_card_and_in_the_issue() -> None:
    # #304: a revision folds in later messages, so the cutoff moves to its time.
    from jotflow.models import IssueProposal
    from jotflow.orchestrator.card_sync import card_context
    from jotflow.store import MemoryStore

    store = MemoryStore(clock=lambda: datetime.fromtimestamp(1792400000, tz=UTC))
    topic = (await store.create_topic(CH, "Progress bar")).topic
    for ts, thread in ((ROOT, None), (REPLY, ROOT), (KIM, ROOT)):
        await store.link_message(MessageRef(channel_id=CH, ts=ts, thread_ts=thread), topic.id)
    late = "1792399000.000100"  # after the draft was made, before its revision
    await store.link_message(MessageRef(channel_id=CH, ts=late, thread_ts=ROOT), topic.id)
    crowd = Crowd()
    crowd._authors[late] = {"U0OUTSIDE"}  # type: ignore[index]
    draft = on_topic(record(MessageRef(channel_id=CH, ts=ROOT)), topic.id).model_copy(
        update={
            "created_at": datetime.fromtimestamp(1792320000, tz=UTC),
            "state": ProposalState.PENDING,
        }
    )
    added = await store.add_proposal(draft)
    assert isinstance(added.proposal, IssueProposal)
    revised = added.proposal.model_copy(
        update={
            "body": added.proposal.body + " More.",
            # The late message reaches the credit through the topic, as a
            # same-topic contribution, so the cutoff decides whether it counts.
            "sources": [MessageRef(channel_id=CH, ts=ROOT)],
        }
    )
    after = await store.revise_proposal(
        added.id, revised, run_id="run-2", base_revision=added.revision, added_by=["U0OUTSIDE"]
    )
    tail = await execution_footer(after, policy(), crowd, store)
    assert "Outsider" in tail.splitlines()[0]
    context = await card_context(policy(), store, Names(), after, attribution=crowd)
    assert context.credit_line is not None and "Outsider" in context.credit_line
