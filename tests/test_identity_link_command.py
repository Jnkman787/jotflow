"""`jotflow identity link`: an admin bulk-loads Slack-to-GitHub links (#305)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from jotflow.commands import identity as command
from jotflow.commands.identity import LinkRefused, load_links, read_pairs
from jotflow.models import Account
from jotflow.orchestrator.launch import GithubBusy
from jotflow.store import MemoryStore

ACCOUNTS = {
    "octo-example": Account(account="user", id=583231, login="octo-example"),
    "kim-example": Account(account="user", id=9001, login="Kim-Example"),
    "acme-co": Account(account="organization", id=77, login="acme-co"),
}


class Runner:
    def __init__(self, busy: bool = False) -> None:
        self.asked: list[str] = []
        self.busy = busy

    async def account(self, login: str) -> Account:
        self.asked.append(login)
        if self.busy:
            raise GithubBusy("GitHub couldn't be asked right now")
        return ACCOUNTS.get(login.casefold(), Account(account="missing"))


class Notes:
    def __init__(self) -> None:
        self.told: list[str] = []

    async def tell(self, user_id: str, text: str) -> None:
        self.told.append(user_id)


def args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(
        **{"member_id": None, "github_login": None, "file": None, "replace": False, **kw}
    )


def csv_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "links.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_file_is_read_and_checked_whole(tmp_path: Path) -> None:
    good = csv_file(
        tmp_path,
        "slack_member_id,github_login\nU0BOB,octo-example\n\nU0KIM,@kim-example\n",
    )
    assert read_pairs(args(file=good)) == [("U0BOB", "octo-example"), ("U0KIM", "kim-example")]
    assert read_pairs(args(member_id="U0BOB", github_login="octo-example")) == [
        ("U0BOB", "octo-example")
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("member,login\nU0BOB,octo-example\n", "first line must be"),
        ("slack_member_id,github_login\nbob,octo-example\n", "line 2: the first column"),
        ("slack_member_id,github_login\nU0BOB,not a login\n", "line 2: the second column"),
        ("slack_member_id,github_login\nU0BOB,dependabot[bot]\n", "a person's GitHub login"),
        ("slack_member_id,github_login\nU0BOB,a,b\n", "exactly two columns"),
        (
            "slack_member_id,github_login\nU0BOB,octo-example\nU0KIM,Octo-Example\n",
            "line 3 repeats",
        ),
    ],
)
def test_a_bad_line_refuses_the_whole_file(tmp_path: Path, text: str, problem: str) -> None:
    with pytest.raises(LinkRefused, match=problem):
        read_pairs(args(file=csv_file(tmp_path, text)))


def test_a_pair_or_a_file_but_not_both() -> None:
    with pytest.raises(LinkRefused):
        read_pairs(args())
    with pytest.raises(LinkRefused):
        read_pairs(args(file=Path("x.csv"), member_id="U0BOB"))


async def test_links_are_stored_as_admin_with_the_account_id() -> None:
    store = MemoryStore()
    counts = await load_links(
        store,
        Runner(),  # type: ignore[arg-type]
        Notes(),  # type: ignore[arg-type]
        [("U0BOB", "octo-example"), ("U0KIM", "kim-example")],
        replace=False,
    )
    assert (counts.linked, counts.unchanged, counts.replaced) == (2, 0, 0)
    kim = await store.get_identity("U0KIM")
    # GitHub's own spelling, and its id, so a later self-add can match the account.
    assert kim is not None and (kim.github_login, kim.github_id, kim.linked_by) == (
        "Kim-Example",
        9001,
        "admin",
    )
    again = await load_links(
        store,
        Runner(),
        Notes(),
        [("U0BOB", "octo-example")],
        replace=False,  # type: ignore[arg-type]
    )
    assert (again.linked, again.unchanged) == (0, 1)


@pytest.mark.parametrize(
    ("login", "problem"),
    [("nobody-here", "no GitHub account"), ("acme-co", "an organization")],
)
async def test_every_login_is_checked_before_anything_is_written(login: str, problem: str) -> None:
    store = MemoryStore()
    with pytest.raises(LinkRefused, match=problem):
        await load_links(
            store,
            Runner(),  # type: ignore[arg-type]
            Notes(),  # type: ignore[arg-type]
            [("U0BOB", "octo-example"), ("U0KIM", login)],
            replace=False,
        )
    assert await store.get_identity("U0BOB") is None  # the good first line wasn't written


async def test_github_busy_links_nothing() -> None:
    store = MemoryStore()
    with pytest.raises(LinkRefused, match="nothing was linked"):
        await load_links(
            store,
            Runner(busy=True),
            Notes(),
            [("U0BOB", "octo-example")],
            replace=False,  # type: ignore[arg-type]
        )


async def test_someone_elses_login_needs_replace_and_they_are_told() -> None:
    store = MemoryStore()
    await store.link_identity("U0SQUAT", "octo-example")
    with pytest.raises(LinkRefused, match="--replace"):
        await load_links(
            store,
            Runner(),
            Notes(),
            [("U0BOB", "octo-example")],
            replace=False,  # type: ignore[arg-type]
        )
    notes = Notes()
    counts = await load_links(
        store,
        Runner(),
        notes,
        [("U0BOB", "octo-example")],
        replace=True,  # type: ignore[arg-type]
    )
    assert (counts.replaced, counts.told) == (1, 1) and notes.told == ["U0SQUAT"]
    assert await store.get_identity("U0SQUAT") is None
    [removed] = await store.identity_unlinks()
    assert removed.removed_by == "operator"


def test_the_command_prints_counts_never_logins(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_link(_: argparse.Namespace, pairs: list[tuple[str, str]]) -> Any:
        return command.LinkCounts(linked=len(pairs))

    monkeypatch.setattr(command, "_link", fake_link)
    code = command.run(
        argparse.Namespace(
            action="link", member_id="U0BOB", github_login="octo-example", file=None, replace=False
        )
    )
    out = capsys.readouterr()
    assert code == command.EXIT_OK
    assert out.out.strip() == "linked 1, unchanged 0, replaced 0"
    assert "octo-example" not in out.out + out.err


def test_a_refusal_names_the_line_not_the_login(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = csv_file(tmp_path, "slack_member_id,github_login\nU0BOB,not a login\n")
    code = command.run(args(action="link", file=path))
    err = capsys.readouterr().err
    assert code == command.EXIT_REFUSED and "line 2" in err and "not a login" not in err


def test_the_card_says_an_admin_made_the_link() -> None:
    from datetime import UTC, datetime

    from jotflow.models import IssueProposal, ProposalRecord
    from jotflow.orchestrator.cards import CardContext, render_card
    from jotflow.policy import parse_policy
    from jotflow.store.base import IdentityLink

    policy = parse_policy(
        '[channels.C0EXAMPLE1]\n[[channels.C0EXAMPLE1.repos]]\nrepo = "acme-co/web"\n'
        'nickname = "web"\ndescription = "Web"\n'
    )
    when = datetime(2026, 10, 29, tzinfo=UTC)
    record = ProposalRecord(
        id="p1",
        channel_id="C0EXAMPLE1",
        thread_ts="1792310400.000100",
        run_id="r1",
        proposal=IssueProposal(
            repo="acme-co/web", title="t", body="b", assignees=["octo-example"], confidence=0.9
        ),
        participants=["U0BOB"],
        created_at=when,
    )
    admin = IdentityLink("U0BOB", "octo-example", False, when, None, 583231, "admin")
    context = CardContext(channel=policy.channel("C0EXAMPLE1"), identities={"U0BOB": admin})
    text = str(render_card(record, context))
    assert "Assignee: octo-example (linked by an admin)" in text
    assert "(unverified)" not in text


def test_a_spreadsheets_byte_order_mark_is_fine(tmp_path: Path) -> None:
    path = tmp_path / "links.csv"
    path.write_bytes("slack_member_id,github_login\nU0BOB,octo-example\n".encode("utf-8-sig"))
    assert read_pairs(args(file=path)) == [("U0BOB", "octo-example")]


def test_a_file_that_isnt_utf8_is_refused_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "links.csv"
    path.write_bytes(b"slack_member_id,github_login\nU0BOB,\xff\xfe\n")
    assert command.run(args(action="link", file=path)) == command.EXIT_REFUSED
    assert "can't read the file (UnicodeDecodeError)" in capsys.readouterr().err
