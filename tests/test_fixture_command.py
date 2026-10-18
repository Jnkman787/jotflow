"""`jotflow execute-fixture` (#55)."""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from jotflow.cli import main
from jotflow.models import ProposalState, RunKind, RunStatus
from jotflow.store import SqliteStore

POLICY = """
[channels.C0TEST1]

[[channels.C0TEST1.repos]]
repo = "acme/web"
nickname = "web"
description = "The web app"
labels = ["bug", "idea"]
"""

FIXTURE: dict[str, Any] = {
    "channel_id": "C0TEST1",
    "thread_ts": "1760781600.000100",
    "repo": "web",
    "title": "Add a dark mode",
    "body": "SECRET-LOOKING BODY TEXT",
    "labels": ["idea"],
    "approved_by": "U0CAROL",
}


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("JOTFLOW_") or name == "CI":
            monkeypatch.delenv(name)
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")
    (tmp_path / "jotflow.toml").write_text(POLICY, encoding="utf-8")
    return tmp_path


def write(workspace: Path, **changes: Any) -> str:
    path = workspace / "fixture.json"
    path.write_text(json.dumps({**FIXTURE, **changes}), encoding="utf-8")
    return str(path)


def stored(workspace: Path) -> tuple[Any, list[Any]]:
    async def read() -> tuple[Any, list[Any]]:
        store = await SqliteStore.open(workspace / "jotflow.db")
        try:
            runs = await store.thread_runs("C0TEST1", FIXTURE["thread_ts"])
            proposal = await store.get_proposal(runs[0].proposal_id or "") if runs else None
            return proposal, runs
        finally:
            await store.close()

    return asyncio.run(read())


def test_a_fixture_is_approved_and_its_run_queued(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["execute-fixture", write(workspace), "--confirm"]) == 0
    out = capsys.readouterr().out
    ids = json.loads(out.strip().splitlines()[-1])
    proposal, runs = stored(workspace)
    assert proposal.id == ids["proposal_id"] and proposal.state is ProposalState.APPROVED
    assert proposal.approved_by == "U0CAROL"
    assert proposal.proposal.repo == "acme/web" and proposal.proposal.labels == ["idea"]
    (run,) = runs
    assert (run.id, run.kind, run.status) == (ids["run_id"], RunKind.EXECUTION, RunStatus.QUEUED)
    assert "SECRET-LOOKING" not in out and "dark mode" not in out


def test_reconciling_reuses_an_id_so_the_run_looks_for_its_marker(workspace: Path) -> None:
    marker = "0123456789abcdef"
    assert main(["execute-fixture", write(workspace, id=marker), "--confirm", "--reconciling"]) == 0
    proposal, _ = stored(workspace)
    assert proposal.id == marker and proposal.state is ProposalState.RECONCILING
    # The same id twice in one database is refused.
    assert main(["execute-fixture", write(workspace, id=marker), "--confirm"]) == 2


def test_standard_input_works(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.dumps(FIXTURE).encode()

    class Stdin:
        buffer = io.BytesIO(raw)

    monkeypatch.setattr("sys.stdin", Stdin)
    assert main(["execute-fixture", "-", "--confirm"]) == 0


@pytest.mark.parametrize(
    ("args", "changes", "env", "problem"),
    [
        ([], {}, {}, "add --confirm"),
        (["--confirm"], {}, {"CI": "true"}, "never runs in CI"),
        (["--confirm"], {"repo": "acme/other"}, {}, "acme/other"),
        (["--confirm"], {"labels": ["wontfix"]}, {}, "wontfix"),
        (["--confirm"], {"channel_id": "C0OTHER1"}, {}, "not in jotflow.toml"),
        (["--confirm"], {"thread_ts": "yesterday"}, {}, "thread_ts"),
        (["--confirm"], {"approved_by": "carol"}, {}, "approved_by"),
        (["--confirm"], {"id": "../etc"}, {}, "id"),
        (["--confirm"], {"command": "rm -rf /"}, {}, "command"),
    ],
)
def test_refusals(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    changes: dict[str, Any],
    env: dict[str, str],
    problem: str,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["execute-fixture", write(workspace, **changes), *args]) == 2
    err = capsys.readouterr().err
    assert problem in err
    assert "SECRET-LOOKING" not in err
    assert not (workspace / "jotflow.db").exists()


def test_an_unreadable_or_oversized_fixture_is_refused(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["execute-fixture", str(workspace / "missing.json"), "--confirm"]) == 2
    assert "could not read" in capsys.readouterr().err
    big = workspace / "big.json"
    big.write_bytes(b" " * (64 * 1024 + 1))
    assert main(["execute-fixture", str(big), "--confirm"]) == 2
    assert "larger than" in capsys.readouterr().err
    bad = workspace / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert main(["execute-fixture", str(bad), "--confirm"]) == 2


def test_configuration_and_database_problems(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(workspace)
    monkeypatch.delenv("JOTFLOW_SLACK_BOT_TOKEN")
    assert main(["execute-fixture", path, "--confirm"]) == 2
    assert "configuration problem" in capsys.readouterr().err
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_DATABASE_PATH", str(workspace / "no" / "such" / "j.db"))
    assert main(["execute-fixture", path, "--confirm"]) == 4
    assert "could not use the database" in capsys.readouterr().err


def test_the_fixture_path_is_required(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["execute-fixture", "--confirm"]) == 2
    assert "give the fixture file" in capsys.readouterr().err
