"""A channel with a scope: cards, re:, replies, rebuild and help see the found repos (#236)."""

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


async def test_help_and_repos_show_found_repos_and_a_line_per_scope() -> None:
    from jotflow.models import ChannelMode
    from jotflow.orchestrator.help import channel_help, channel_repos
    from jotflow.orchestrator.help_blocks import help_reply, repos_reply

    repos = await catalog(
        [
            found(1, "web", description="The storefront <!here>", topics=["shop"]),
            found(2, "web-admin", description="Back office"),
        ]
    )
    text = channel_help(POLICY, CH, ChannelMode.PROPOSE, repos)
    assert "This channel feeds: docs (acme-co/docs), web-admin (acme-co/web-admin)" in text
    assert "Also every repo in acme-co matching web*" in text
    _, blocks = help_reply(POLICY, CH, ChannelMode.PROPOSE, repos)
    star = chr(0x2217)  # the lookalike: a glob's * doesn't turn the line bold
    assert f"every repo in acme-co matching web{star}" in str(blocks)
    listed = channel_repos(POLICY, CH, "", repos)
    assert "  web: acme-co/web. The storefront" in listed  # GitHub's text, cleaned
    assert "<!here>" not in listed
    assert channel_repos(POLICY, CH, "shop", repos).splitlines()[1:2] == [
        "  web: acme-co/web. The storefront"
    ]
    reply, blocks = repos_reply(POLICY, CH, "", repos)
    assert reply == listed and "<!here>" not in str(blocks)


async def test_a_thousand_found_repos_fit_in_one_reply() -> None:
    from jotflow.orchestrator.help_blocks import MAX_BLOCKS, repos_reply

    repos = await catalog([found(n, f"web-{n:04d}", description="x" * 150) for n in range(1, 1001)])
    text, blocks = repos_reply(POLICY, CH, "", repos)
    assert len(blocks) <= MAX_BLOCKS and len(text) <= 39_000
    assert "more; add a word to narrow the list" in text
    assert text.splitlines()[-1].startswith("Also every repo in acme-co")


# Globs ignore case, as GitHub's repo names do (#236, security's review)

CASED = Policy.model_validate(
    {
        "channels": {
            CH: {
                "repos": [{"repo": "acme-co/docs", "nickname": "docs", "description": "Docs"}],
                "scopes": [{"owner": "acme-co", "match": ["*"], "exclude": ["payroll*"]}],
            }
        }
    }
)


async def cased_catalog() -> RepoCatalog:
    listing = [found(1, "Payroll-api"), found(2, "PAYROLL"), found(3, "Web-Shop")]

    async def lister(owner: str) -> list[DiscoveredRepo]:
        return list(listing)

    repos = RepoCatalog(CASED, lister=lister, access=lambda o, n: _yes())
    await repos.refresh("acme-co")
    return repos


async def _yes() -> bool:
    return True


def test_a_glob_ignores_case_in_match_and_exclude() -> None:
    scope = CASED.channels[CH].scopes[0]
    assert not scope.covers("Payroll-api") and not scope.covers("PAYROLL")
    assert scope.covers("Web-Shop")
    upper = scope.model_copy(update={"match": ("WEB-*",), "exclude": ()})
    assert upper.covers("web-shop") and upper.covers("Web-Shop")
    assert not CASED.channels[CH].reaches("ACME-CO/Payroll-api")
    assert CASED.channels[CH].reaches("ACME-CO/web-shop")


def test_the_owner_check_ignores_case_and_refuses_odd_names() -> None:
    from jotflow.models import under_owner

    assert under_owner("Acme-Co/web", "acme-co")
    assert not under_owner("acme-co-x/web", "acme-co")
    assert not under_owner("acme-cö/web", "acme-cö")  # not a GitHub owner
    assert not under_owner("acme-co/..", "acme-co") and not under_owner("acme-co", "acme-co")


async def test_an_excluded_repo_with_capitals_is_never_found_proposed_or_approved() -> None:
    from jotflow.policy import RepoNotAllowedError

    repos = await cased_catalog()
    # Discovery: neither payroll repo is listed, whatever its case.
    assert [r.repo for r in repos.repos(CH)] == ["acme-co/docs", "acme-co/Web-Shop"]
    with pytest.raises(RepoNotAllowedError):
        repos.repo(CH, "acme-co/Payroll-api")
    # Approval: a proposal for it, however it was stored, is refused at the re-check.
    store = MemoryStore()
    slack = Slack()
    handler = ApprovalHandler(store, CASED, slack, repos=repos)
    rec = record("acme-co/Payroll-api")
    await store.add_proposal(rec)
    await store.set_card_ts(rec.id, CARD_TS)
    from jotflow.store import content_hash
    from tests.test_approval import click

    value = f"{rec.id}:{content_hash(rec.proposal)}"
    assert await handler.on_approve(click(value)) == "refused"
    assert "can't be used here any more" in slack.ephemerals[-1][2]
