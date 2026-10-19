from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jotflow.models import (
    ExecutionResult,
    IssueChanges,
    IssueProposal,
    MessageRef,
    ProposalRecord,
    ProposalState,
    UpdateProposal,
)
from jotflow.orchestrator.cards import (
    ACTION_APPROVE,
    ACTION_ASSIGN,
    ACTION_REPO,
    ACTION_REQUEST_CHANGES,
    ACTION_UNASSIGN,
    FAILED_LINE,
    MAX_BLOCKS,
    MAX_BUTTON_TEXT,
    MAX_HEADER_TEXT,
    MAX_SECTION_TEXT,
    SHORTENED,
    TOO_LONG,
    Card,
    CardContext,
    CardPublisher,
    SlackCardPoster,
    render_card,
)
from jotflow.policy import ChannelPolicy
from jotflow.store import MemoryStore, NotFoundError, SqliteStore, Store, content_hash
from jotflow.store.base import IdentityLink

CH = "C0IDEAS01"
THREAD = "1760781600.000100"
NOW = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
INJECTION = "<!channel> <@U0EVIL> <https://evil.example|click> *bold*"
PERMALINK = "https://acme-co.slack.com/archives/C0IDEAS01/p1760781600000100"

TWO_REPOS = ChannelPolicy.model_validate(
    {
        "repos": [
            {"repo": "acme-co/web", "nickname": "web", "description": "Web", "labels": ["bug"]},
            {"repo": "acme-co/api", "nickname": "api", "description": "API"},
        ]
    }
)
ONE_REPO = ChannelPolicy.model_validate(
    {"repos": [{"repo": "acme-co/web", "nickname": "web", "description": "Web"}]}
)


def link(user: str, login: str, verified: bool = False) -> IdentityLink:
    return IdentityLink(user, login, verified, NOW)


def context(**overrides: Any) -> CardContext:
    fields: dict[str, Any] = {
        "channel": TWO_REPOS,
        "names": {"U0ALICE": "Alice", "U0BOB": "Bob", "U0CAROL": "Carol"},
        "identities": {
            "U0ALICE": link("U0ALICE", "acme-alice", verified=True),
            "U0BOB": link("U0BOB", "acme-bob"),
        },
        "visibility": {"acme-co/web": "private"},
    }
    fields.update(overrides)
    return CardContext(**fields)


def issue(**overrides: Any) -> IssueProposal:
    fields: dict[str, Any] = {
        "repo": "acme-co/web",
        "title": "Onboarding progress bar",
        "body": "Add a progress bar to onboarding.",
        "labels": ["bug"],
        "assignees": ["acme-alice"],
        "size": "s",
        "confidence": 0.82,
        "sources": [MessageRef(channel_id=CH, ts=THREAD, permalink=PERMALINK)],
    }
    fields.update(overrides)
    return IssueProposal.model_validate(fields)


def record(
    proposal: IssueProposal | UpdateProposal | None = None, **overrides: Any
) -> ProposalRecord:
    fields: dict[str, Any] = {
        "id": "p1",
        "channel_id": CH,
        "thread_ts": THREAD,
        "run_id": "run-1",
        "proposal": proposal or issue(),
        "participants": ["U0ALICE", "U0BOB", "U0CAROL"],
        "created_at": NOW,
    }
    fields.update(overrides)
    return ProposalRecord.model_validate(fields)


def walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def texts(card: Card, kind: str) -> list[str]:
    return [o["text"] for o in walk(card.blocks) if o.get("type") == kind and "text" in o]


def actions(card: Card) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for obj in walk(card.blocks):
        if "action_id" in obj:
            found.setdefault(obj["action_id"], []).append(obj)
    return found


def all_text(card: Card) -> str:
    return json.dumps(card.blocks)


# --- create proposals ---------------------------------------------------------


def test_a_create_card_shows_everything_approved() -> None:
    card = render_card(record(), context())
    plain = texts(card, "plain_text")
    assert "Proposed issue" in plain
    assert "Onboarding progress bar" in plain
    assert "Add a progress bar to onboarding." in plain
    assert "Labels: bug" in plain
    assert "Size: s   Confidence: 82%" in plain
    assert card.text == "Proposed issue for acme-co/web"
    approve = actions(card)[ACTION_APPROVE][0]
    assert approve["value"] == f"p1:{content_hash(issue())}"
    assert approve["style"] == "primary"
    assert actions(card)[ACTION_REQUEST_CHANGES][0]["value"] == "p1"


def test_agent_text_only_ever_appears_as_plain_text() -> None:
    proposal = issue(title=INJECTION, body=INJECTION * 3, labels=[INJECTION], size=INJECTION)
    card = render_card(record(proposal), context())
    for text in texts(card, "mrkdwn"):
        assert "<!channel>" not in text and "evil.example" not in text and "*bold*" not in text
    assert any("<!channel>" in t for t in texts(card, "plain_text"))
    for obj in walk(card.blocks):
        if obj.get("type") == "plain_text":
            assert obj["emoji"] is False
    assert "<!channel>" not in card.text


def test_long_bodies_are_shown_in_full_across_sections() -> None:
    body = "x" * (MAX_SECTION_TEXT * 2 + 10)
    card = render_card(record(issue(body=body)), context())
    pieces = [t for t in texts(card, "plain_text") if set(t) == {"x"}]
    assert "".join(pieces) == body
    assert all(len(p) <= MAX_SECTION_TEXT for p in pieces)
    assert ACTION_APPROVE in actions(card)


def test_a_body_too_long_for_one_card_cannot_be_approved() -> None:
    body = "y" * (MAX_SECTION_TEXT * MAX_BLOCKS)
    card = render_card(record(issue(body=body)), context())
    assert len(card.blocks) <= MAX_BLOCKS
    assert ACTION_APPROVE not in actions(card)
    assert ACTION_REQUEST_CHANGES in actions(card)
    assert TOO_LONG in texts(card, "plain_text")
    assert "Onboarding progress bar" in texts(card, "plain_text")


def test_slack_size_limits_hold() -> None:
    proposal = issue(title="t" * 256)
    card = render_card(record(proposal), context(names={"U0ALICE": "n" * 500}))
    for block in card.blocks:
        if block["type"] == "header":
            assert len(block["text"]["text"]) <= MAX_HEADER_TEXT
    for obj in walk(card.blocks):
        if obj.get("type") == "button":
            assert len(obj["text"]["text"]) <= MAX_BUTTON_TEXT
        if obj.get("type") == "plain_text":
            assert 1 <= len(obj["text"]) <= MAX_SECTION_TEXT


# --- repo, visibility, labels -------------------------------------------------


def test_the_repo_dropdown_lists_the_channels_repos() -> None:
    card = render_card(record(), context())
    (select,) = actions(card)[ACTION_REPO]
    assert select["initial_option"]["value"] == "acme-co/web"
    assert [o["value"] for o in select["options"]] == ["acme-co/web", "acme-co/api"]


def test_one_repo_needs_no_dropdown() -> None:
    card = render_card(record(), context(channel=ONE_REPO))
    assert ACTION_REPO not in actions(card)
    assert "Repo: acme-co/web" in texts(card, "plain_text")


@pytest.mark.parametrize(
    ("visibility", "line"),
    [
        ("public", "Warning: acme-co/web is public, so this issue will be visible to anyone."),
        ("unknown", "Repo visibility was not checked."),
    ],
)
def test_repo_visibility(visibility: str, line: str) -> None:
    card = render_card(record(), context(visibility={"ACME-CO/web": visibility}))
    assert line in texts(card, "plain_text")


def test_a_private_repo_shows_no_warning() -> None:
    plain = texts(render_card(record(), context()), "plain_text")
    assert not any("visib" in t for t in plain)


def test_labels_without_a_list_are_marked_not_checked() -> None:
    card = render_card(record(issue(repo="acme-co/api", labels=["perf"])), context())
    assert "Labels: perf (not checked against a list)" in texts(card, "plain_text")
    card = render_card(record(issue(repo="acme-co/api", labels=[])), context())
    assert "Labels: none" in texts(card, "plain_text")


# --- people -------------------------------------------------------------------


def test_assignees_and_involved_people() -> None:
    proposal = issue(assignees=["acme-alice", "acme-bob", "acme-zed"])
    card = render_card(record(proposal), context())
    plain = texts(card, "plain_text")
    assert "Assignee: acme-alice" in plain
    assert "Assignee: acme-bob (unverified)" in plain
    assert "Assignee: acme-zed (not linked to anyone in Slack)" in plain
    assert {"Involved: Alice", "Involved: Bob", "Involved: Carol"} <= set(plain)
    removes = [b["value"] for b in actions(card)[ACTION_UNASSIGN]]
    assert removes == ["p1:acme-alice", "p1:acme-bob", "p1:acme-zed"]
    assert ACTION_ASSIGN not in actions(card)  # Alice and Bob already assigned; Carol unlinked


def test_an_involved_linked_person_can_be_made_assignee() -> None:
    card = render_card(record(issue(assignees=[])), context())
    assert [b["value"] for b in actions(card)[ACTION_ASSIGN]] == ["p1:U0ALICE", "p1:U0BOB"]


# --- sources ------------------------------------------------------------------


def test_sources_link_only_slack_permalinks() -> None:
    sources = [
        MessageRef(channel_id=CH, ts="1.1", permalink=PERMALINK),
        MessageRef(channel_id=CH, ts="1.2", permalink="https://evil.example/archives/C1/p1"),
        MessageRef(channel_id=CH, ts="1.3"),
    ]
    card = render_card(record(issue(sources=sources)), context())
    (line,) = [t for t in texts(card, "mrkdwn") if t.startswith("Drawn from")]
    assert line == f"Drawn from <{PERMALINK}|message 1>"
    assert "evil.example" not in all_text(card)


def test_no_sources_line_without_permalinks() -> None:
    card = render_card(record(issue(sources=[])), context())
    assert not any(t.startswith("Drawn from") for t in texts(card, "mrkdwn"))


# --- update proposals ---------------------------------------------------------


def test_an_update_card() -> None:
    update = UpdateProposal(
        topic_id="t1",
        repo="acme-co/web",
        issue_number=42,
        changes=IssueChanges(
            body="New description",
            comment=INJECTION,
            add_labels=["bug"],
            remove_labels=["wontfix"],
            add_assignees=["acme-bob"],
            remove_assignees=["acme-zed"],
        ),
        confidence=0.6,
    )
    card = render_card(record(update), context())
    plain = texts(card, "plain_text")
    assert "Proposed update to web#42" in plain
    assert {"New description:", "New description", "Comment:", INJECTION} <= set(plain)
    assert "Add labels: bug" in plain
    assert "Remove labels: wontfix" in plain
    assert "Unassign: acme-zed" in plain
    assert "Assignee: acme-bob (unverified)" in plain
    assert "Confidence: 60%" in plain
    assert actions(card)[ACTION_APPROVE][0]["value"] == f"p1:{content_hash(update)}"


def test_an_update_with_too_long_a_body_cannot_be_approved() -> None:
    update = UpdateProposal(
        topic_id="t1",
        repo="acme-co/api",
        issue_number=7,
        changes=IssueChanges(body="z" * (MAX_SECTION_TEXT * MAX_BLOCKS), comment="hi"),
        confidence=0.5,
    )
    card = render_card(record(update), context())
    assert ACTION_APPROVE not in actions(card)
    assert texts(card, "plain_text").count(TOO_LONG) == 2


# --- states -------------------------------------------------------------------


def _state(state: ProposalState, **fields: Any) -> Card:
    return render_card(record(state=state, **fields), context())


def test_only_a_pending_card_has_buttons() -> None:
    card = _state(ProposalState.REJECTED)
    assert not any(a in actions(card) for a in (ACTION_APPROVE, ACTION_REPO, ACTION_UNASSIGN))
    assert "Rejected. Nothing was written." in texts(card, "plain_text")


def test_approved_by_a_person() -> None:
    card = _state(ProposalState.APPROVED, approved_by="U0CAROL", approved_at=NOW, content_hash="x")
    assert "Approved by <@U0CAROL> 2026-10-18 09:30 UTC" in texts(card, "mrkdwn")


def test_approved_by_auto_mode() -> None:
    card = _state(ProposalState.APPROVED, approved_by="auto", approved_at=NOW, content_hash="x")
    assert "Approved by auto mode 2026-10-18 09:30 UTC" in texts(card, "plain_text")


def test_an_odd_approver_is_not_rendered_as_markup() -> None:
    card = _state(ProposalState.APPROVED, approved_by="<!here>", content_hash="x")
    assert "Approved" in texts(card, "plain_text")
    assert "<!here>" not in all_text(card)


@pytest.mark.parametrize(
    ("state", "line"),
    [
        (ProposalState.EXECUTING, "Creating it on GitHub now."),
        (ProposalState.RECONCILING, "checking whether it already happened"),
        (ProposalState.SUPERSEDED, "Replaced by a newer proposal in this thread."),
    ],
)
def test_state_lines(state: ProposalState, line: str) -> None:
    assert any(line in t for t in texts(_state(state, content_hash="x"), "plain_text"))


def test_applied_links_the_issue() -> None:
    card = _state(
        ProposalState.APPLIED,
        content_hash="x",
        result=ExecutionResult(issue_url="https://github.com/acme-co/web/issues/42"),
    )
    assert "Done: <https://github.com/acme-co/web/issues/42|acme-co/web#42>" in texts(
        card, "mrkdwn"
    )


def test_failed_does_not_show_the_agents_reason() -> None:
    card = _state(
        ProposalState.FAILED,
        content_hash="x",
        result=ExecutionResult(error="private-repo-secret " + INJECTION),
    )
    assert FAILED_LINE in texts(card, "plain_text")
    assert "private-repo-secret" not in all_text(card)


def test_a_cleared_draft_shows_no_text() -> None:
    cleared = issue(title="(cleared)", body="")
    card = _state(ProposalState.REJECTED, proposal=cleared, draft_cleared_at=NOW)
    plain = texts(card, "plain_text")
    assert "(cleared)" not in plain
    assert "Rejected. Nothing was written. The drafted text has been cleared." in plain


# --- publishing ---------------------------------------------------------------


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, Card]] = []
        self.updates: list[tuple[str, str, Card]] = []

    async def post(self, channel_id: str, thread_ts: str, card: Card) -> str:
        self.posts.append((channel_id, thread_ts, card))
        return f"1760781700.{len(self.posts):06d}"

    async def update(self, channel_id: str, ts: str, card: Card) -> None:
        self.updates.append((channel_id, ts, card))


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    made: Store = (
        MemoryStore()
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db")
    )
    yield made
    await made.close()


async def test_the_card_is_posted_once_then_updated_in_place(store: Store) -> None:
    poster = FakePoster()
    publisher = CardPublisher(store, poster)
    added = await store.add_proposal(record())
    ts = await publisher.show(added, context())
    assert [(c, t) for c, t, _ in poster.posts] == [(CH, THREAD)]
    stored = await store.get_proposal("p1")
    assert stored is not None and stored.card_ts == ts

    approved = await store.transition(
        "p1", ProposalState.APPROVED, approved_by="U0CAROL", expected_hash=content_hash(issue())
    )
    assert approved.card_ts == ts
    assert await publisher.show(approved, context()) == ts
    assert len(poster.posts) == 1
    (channel, updated_ts, card) = poster.updates[0]
    assert (channel, updated_ts) == (CH, ts)
    assert ACTION_APPROVE not in actions(card)


async def test_set_card_ts_rules(store: Store) -> None:
    with pytest.raises(NotFoundError):
        await store.set_card_ts("missing", "1.1")
    await store.add_proposal(record())
    with pytest.raises(ValueError):
        await store.set_card_ts("p1", "")
    with pytest.raises(ValueError, match="no card yet"):
        await store.add_proposal(record(id="p2", card_ts="1.1"))


async def test_slack_card_poster_uses_post_and_update() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Client:
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("post", kwargs))
            return {"ok": True, "ts": "1760781700.000001"}

        async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("update", kwargs))
            return {"ok": True}

    poster = SlackCardPoster(Client())
    card = Card(blocks=[{"type": "divider"}], text="Proposed issue for acme-co/web")
    assert await poster.post(CH, THREAD, card) == "1760781700.000001"
    await poster.update(CH, "1760781700.000001", card)
    assert calls == [
        ("post", {"channel": CH, "thread_ts": THREAD, "blocks": card.blocks, "text": card.text}),
        (
            "update",
            {"channel": CH, "ts": "1760781700.000001", "blocks": card.blocks, "text": card.text},
        ),
    ]


def test_a_repo_the_channel_does_not_list_is_shown_plainly() -> None:
    update = UpdateProposal(
        topic_id="t1",
        repo="acme-co/other",
        issue_number=3,
        changes=IssueChanges(add_labels=["x"]),
        confidence=0.4,
    )
    card = render_card(record(update), context())
    plain = texts(card, "plain_text")
    assert "Proposed update to acme-co/other#3" in plain
    assert "Repo: acme-co/other" in plain
    assert ACTION_REPO not in actions(card)
    assert "Add labels: x (not checked against a list)" in plain


def test_no_size_means_only_confidence() -> None:
    card = render_card(record(issue(size=None)), context())
    assert "Confidence: 82%" in texts(card, "plain_text")


def test_a_cleared_update_and_one_with_only_labels() -> None:
    bare = UpdateProposal(
        topic_id="t1", repo="acme-co/web", issue_number=9, changes=IssueChanges(), confidence=0.5
    )
    plain = texts(render_card(record(bare), context()), "plain_text")
    assert "New description:" not in plain and "Comment:" not in plain
    full = bare.model_copy(update={"changes": IssueChanges(body="b", comment="c")})
    cleared = texts(
        render_card(record(full, state=ProposalState.SUPERSEDED, draft_cleared_at=NOW), context()),
        "plain_text",
    )
    assert "b" not in cleared and "c" not in cleared


def test_many_participants_are_compacted_and_the_card_still_fits() -> None:
    people = [f"U0P{i:03d}" for i in range(60)]
    card = render_card(record(participants=people), context())
    assert len(card.blocks) <= MAX_BLOCKS
    assert ACTION_APPROVE in actions(card)
    assert ACTION_ASSIGN not in actions(card)  # compacted: no per-person buttons
    assert any(t.startswith("Involved: U0P000, U0P001") for t in texts(card, "plain_text"))
    assert "Assignees: acme-alice" in texts(card, "plain_text")


def test_shortened_approved_content_cannot_be_approved() -> None:
    labels = [f"label-{i:02d}-" + "x" * 40 for i in range(60)]
    card = render_card(record(issue(repo="acme-co/api", labels=labels)), context())
    assert ACTION_APPROVE not in actions(card)
    assert SHORTENED in texts(card, "plain_text")
    assert ACTION_REQUEST_CHANGES in actions(card)


def test_a_huge_body_and_many_people_still_fit() -> None:
    people = [f"U0P{i:03d}" for i in range(60)]
    body = "y" * (MAX_SECTION_TEXT * MAX_BLOCKS)
    card = render_card(record(issue(body=body), participants=people), context())
    assert len(card.blocks) <= MAX_BLOCKS
    assert ACTION_APPROVE not in actions(card)
    assert TOO_LONG in texts(card, "plain_text")


def test_a_huge_body_with_nobody_involved() -> None:
    body = "y" * (MAX_SECTION_TEXT * MAX_BLOCKS)
    card = render_card(record(issue(body=body, assignees=[]), participants=[]), context())
    assert len(card.blocks) <= MAX_BLOCKS
    assert not any(t.startswith(("Involved", "Assignee")) for t in texts(card, "plain_text"))


def test_a_failed_card_offers_retry_with_the_hash_and_discard() -> None:
    from jotflow.orchestrator.cards import ACTION_DISCARD, ACTION_RETRY

    failed = record(state=ProposalState.FAILED, approved_by="U0CAROL", approved_at=NOW)
    found = actions(render_card(failed, context()))
    assert [b["value"] for b in found[ACTION_RETRY]] == [f"p1:{content_hash(failed.proposal)}"]
    assert [b["value"] for b in found[ACTION_DISCARD]] == ["p1"]
    assert found[ACTION_DISCARD][0]["style"] == "danger"
    # Once discarded, the draft is gone and so are both buttons.
    cleared = failed.model_copy(update={"draft_cleared_at": NOW})
    gone = actions(render_card(cleared, context()))
    assert ACTION_RETRY not in gone and ACTION_DISCARD not in gone


def test_a_failed_card_too_long_to_show_offers_only_discard() -> None:
    from jotflow.orchestrator.cards import ACTION_DISCARD, ACTION_RETRY

    long = record(
        issue(body="y" * (MAX_SECTION_TEXT * MAX_BLOCKS)),
        state=ProposalState.FAILED,
        approved_by="U0C",
    )
    found = actions(render_card(long, context()))
    assert ACTION_RETRY not in found and ACTION_DISCARD in found


# --- an overruled keyword hint (#147) -------------------------------------------


def _update(**overrides: Any) -> UpdateProposal:
    fields: dict[str, Any] = {
        "topic_id": "t1",
        "repo": "acme-co/web",
        "issue_number": 42,
        "changes": IssueChanges(comment="Keep the column order."),
        "confidence": 0.7,
    }
    fields.update(overrides)
    return UpdateProposal(**fields)


REASON = "The thread describes a crash on save, so this is a bug, not an idea."


def test_a_create_card_shows_why_the_hint_was_overruled() -> None:
    card = render_card(record(issue(hint_overruled=REASON)), context())
    plain = texts(card, "plain_text")
    line = f"Hint overruled: {REASON}"
    assert line in plain
    # Just after the confidence line, where people read how sure the agent is.
    assert plain.index(line) == plain.index("Size: s   Confidence: 82%") + 1
    assert ACTION_APPROVE in actions(card)


def test_an_update_card_shows_why_the_hint_was_overruled() -> None:
    card = render_card(record(_update(hint_overruled=REASON)), context())
    plain = texts(card, "plain_text")
    assert plain.index(f"Hint overruled: {REASON}") == plain.index("Confidence: 70%") + 1


@pytest.mark.parametrize("proposal", [issue(), _update()], ids=["issue", "update"])
def test_without_an_overruled_hint_the_card_is_unchanged(
    proposal: IssueProposal | UpdateProposal,
) -> None:
    card = render_card(record(proposal), context())
    assert "Hint overruled" not in all_text(card)
    with_hint = render_card(
        record(proposal.model_copy(update={"hint_overruled": REASON})), context()
    )
    assert len(with_hint.blocks) == len(card.blocks) + 1


def test_an_overruled_hint_is_only_ever_plain_text() -> None:
    card = render_card(record(issue(hint_overruled=INJECTION)), context())
    for text in texts(card, "mrkdwn"):
        assert "<!channel>" not in text and "evil.example" not in text
    assert f"Hint overruled: {INJECTION}" in texts(card, "plain_text")


def test_the_overruled_hint_is_part_of_what_is_approved() -> None:
    plain_issue, hinted = issue(), issue(hint_overruled=REASON)
    assert content_hash(plain_issue) != content_hash(hinted)
    approve = actions(render_card(record(hinted), context()))[ACTION_APPROVE][0]
    assert approve["value"] == f"p1:{content_hash(hinted)}"
