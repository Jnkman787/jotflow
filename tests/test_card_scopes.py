"""Cards for repos a channel's scope found: the warning, the dropdown, the edit (#236)."""

from __future__ import annotations

from typing import Any

import pytest

from jotflow.models import DiscoveredRepo, IssueProposal
from jotflow.orchestrator.approval import ApprovalHandler, Refused
from jotflow.orchestrator.card_edits import CardEdits
from jotflow.orchestrator.card_sync import card_context
from jotflow.orchestrator.cards import ACTION_REPO, MAX_OPTIONS, render_card
from jotflow.policy import Policy
from jotflow.repo_catalog import RepoCatalog
from jotflow.store import MemoryStore
from tests.test_approval import CARD_TS, Slack
from tests.test_card_sync import FakePeople
from tests.test_runs import CH, _proposal_record

POLICY = Policy.model_validate(
    {
        "channels": {
            CH: {
                "repos": [{"repo": "acme-co/docs", "nickname": "docs", "description": "Docs"}],
                "scopes": [{"owner": "acme-co", "match": ["web*"]}],
            }
        }
    }
)


def found(n: int, name: str, **fields: Any) -> DiscoveredRepo:
    return DiscoveredRepo.model_validate(
        {
            "id": n,
            "full_name": f"acme-co/{name}",
            "pushed_at": f"2026-10-{1 + n % 28:02d}T00:00:00Z",
            **fields,
        }
    )


async def catalog(repos: list[DiscoveredRepo]) -> RepoCatalog:
    async def lister(owner: str) -> list[DiscoveredRepo]:
        return list(repos)

    catalog = RepoCatalog(POLICY, lister=lister)
    await catalog.refresh("acme-co")
    return catalog


def record(repo: str = "acme-co/web") -> Any:
    base = _proposal_record("0123456789abcdef")
    return base.model_copy(update={"proposal": base.proposal.model_copy(update={"repo": repo})})


def picker(card: Any) -> dict[str, Any] | None:
    for block in card.blocks:
        accessory = block.get("accessory") or {}
        if accessory.get("action_id") == ACTION_REPO:
            return accessory
    return None


async def test_a_found_repo_warns_unless_github_says_private() -> None:
    store = MemoryStore()
    repos = await catalog([found(1, "web", private=True), found(2, "web-site", private=False)])
    private = await card_context(POLICY, store, FakePeople(), record("acme-co/web"), repos)
    public = await card_context(POLICY, store, FakePeople(), record("acme-co/web-site"), repos)
    assert private.repo_visibility("acme-co/web") == "private"
    assert public.repo_visibility("acme-co/web-site") == "public"
    # Gone from the listing since it was drafted: warn rather than guess.
    gone = await card_context(POLICY, store, FakePeople(), record("acme-co/web-old"), repos)
    assert gone.repo_visibility("acme-co/web-old") == "public"


async def test_the_dropdown_offers_found_repos_and_stays_within_slacks_limit() -> None:
    store = MemoryStore()
    many = [found(1, "web", private=True), *[found(n, f"web-{n}") for n in range(2, 140)]]
    repos = await catalog(many)
    rec = record("acme-co/web")
    context = await card_context(POLICY, store, FakePeople(), rec, repos)
    accessory = picker(render_card(rec, context))
    assert accessory is not None
    values = [o["value"] for o in accessory["options"]]
    assert len(values) == MAX_OPTIONS
    # The current repo, pushed longest ago, is kept ahead of the listed ones.
    assert values[0].endswith(":web") and values[1].endswith(":docs")
    assert accessory["initial_option"]["value"].endswith(":web")
    assert accessory["initial_option"] in accessory["options"]


async def test_picking_a_found_repo_resolves_it_and_an_archived_one_is_refused() -> None:
    store = MemoryStore()
    repos = await catalog(
        [found(1, "web"), found(2, "web-site"), found(3, "web-old", archived=True)]
    )
    slack = Slack()
    handler = ApprovalHandler(store, POLICY, slack, repos=repos)
    edits = CardEdits(store, POLICY, handler.guarded, slack, repos=repos)
    rec = record("acme-co/web")
    assert isinstance(rec.proposal, IssueProposal)
    await store.add_proposal(rec)
    await store.set_card_ts(rec.id, CARD_TS)
    assert edits._channel_repo(CH, "web-site").repo == "acme-co/web-site"
    for refused in ("web-old", "acme-co/web-site", "billing"):
        with pytest.raises(Refused):
            edits._channel_repo(CH, refused)


# The other places a channel's repos are named (#236)


async def test_re_names_a_found_repo_and_a_refusal_lists_only_a_few() -> None:
    from jotflow.orchestrator.keywords import ReferenceError, parse_reference

    repos = await catalog([found(n, f"web-{n}") for n in range(1, 20)])
    channel = POLICY.channel(CH)
    assert parse_reference("web-3#7 more", channel, repos.repos(CH)).repo.repo == "acme-co/web-3"
    with pytest.raises(ReferenceError, match=r"say which, such as .* and 15 more$"):
        parse_reference("7", channel, repos.repos(CH))
    with pytest.raises(ReferenceError, match=r"and 15 more\)$"):
        parse_reference("billing#7", channel, repos.repos(CH))


def test_rebuild_keeps_replies_for_repos_a_scope_reaches() -> None:
    channel = POLICY.channel(CH)
    assert channel.reaches("acme-co/docs") and channel.reaches("ACME-co/web-x")
    assert not channel.reaches("acme-co/billing") and not channel.reaches("other/web")


async def test_the_thread_reply_uses_a_found_repos_nickname() -> None:
    from jotflow.models import ExecutionResult
    from jotflow.orchestrator.thread_notice import reply_text

    repos = await catalog([found(1, "web-site")])
    rec = record("acme-co/web-site")
    url = "https://github.com/acme-co/web-site/issues/4"
    rec = rec.model_copy(update={"result": ExecutionResult(issue_url=url)})
    assert reply_text(POLICY, rec, repos) == f"Created <{url}|web-site#4>"
    assert reply_text(POLICY, rec) == f"Created <{url}|acme-co/web-site#4>"
