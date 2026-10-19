"""`jotflow identity unlink`: whoever runs JotFlow removes someone's link (#173)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from jotflow.cli import main
from jotflow.commands import identity as command
from jotflow.store import OPERATOR_REMOVER, SqliteStore


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("JOTFLOW_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    return tmp_path


class Posts:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("messages tab off")
        self.sent.append(kwargs)
        return {"ok": True}


def link(workspace: Path, user: str, login: str) -> None:
    async def go() -> None:
        store = await SqliteStore.open(workspace / "jotflow.db")
        try:
            await store.link_identity(user, login)
        finally:
            await store.close()

    asyncio.run(go())


def records(workspace: Path) -> list[Any]:
    async def go() -> list[Any]:
        store = await SqliteStore.open(workspace / "jotflow.db")
        try:
            return [(await store.get_identity("U0BOB")), *(await store.identity_unlinks())]
        finally:
            await store.close()

    return asyncio.run(go())


@pytest.mark.parametrize("fail", [False, True])
def test_the_host_unlinks_and_tells_the_person(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail: bool,
) -> None:
    posts = Posts(fail)
    monkeypatch.setattr(command, "AsyncWebClient", lambda token: posts)
    link(workspace, "U0BOB", "octo-cat")
    assert main(["identity", "unlink", "U0BOB"]) == 0
    out = capsys.readouterr().out
    if fail:
        assert "couldn't message them" in out
    else:
        assert "they were told privately" in out
        [post] = posts.sent
        assert post["channel"] == "U0BOB"
        assert post["text"].startswith("Whoever runs JotFlow removed")
    assert "octo-cat" not in out
    gone, record = records(workspace)
    assert gone is None
    assert (record.slack_user_id, record.removed_by) == ("U0BOB", OPERATOR_REMOVER)


def test_nothing_to_unlink_says_so(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(command, "AsyncWebClient", lambda token: Posts())
    assert main(["identity", "unlink", "U0NOBODY"]) == 0
    assert "U0NOBODY has no linked GitHub login" in capsys.readouterr().out


@pytest.mark.parametrize("typed", ["bob", "@bob", "u0bob"])
def test_a_name_is_refused(workspace: Path, capsys: pytest.CaptureFixture[str], typed: str) -> None:
    assert main(["identity", "unlink", typed]) == 2
    assert "give a Slack member ID" in capsys.readouterr().err


def test_no_action_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["identity"]) == 2
    assert "usage: jotflow identity" in capsys.readouterr().err


def test_missing_settings_are_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("JOTFLOW_SLACK_BOT_TOKEN")
    assert main(["identity", "unlink", "U0BOB"]) == 2
    assert "configuration problem" in capsys.readouterr().err
