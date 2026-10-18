from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from jotflow import replay as module
from jotflow.commands import replay as command
from jotflow.models import ProposalState, RunKind
from jotflow.replay import NOT_RUN_KEYWORDS, ReplayReport, replay
from jotflow.store import MemoryStore, SqliteStore
from jotflow.testing.threads import builtin_paths, builtin_thread, load_thread, parse_thread
from jotflow.whiteboard.tokens import Grant

PATHS = sorted(builtin_paths())


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("replay must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.mark.parametrize("path", PATHS, ids=[p.stem for p in PATHS])
async def test_every_fixture_replays_to_its_expected_outcome(path: Path, no_network: None) -> None:
    report = await replay(load_thread(path))
    assert report.ok, report.render()
    names = [c.name for c in report.checks]
    for name in (
        "script",
        "ingest accepted",
        "ingest ignored",
        "topics",
        "chatter",
        "proposals",
        "asks",
    ):
        assert name in names


async def test_keyword_checks_are_reported_as_not_run() -> None:
    report = await replay(builtin_thread("keywords"))
    (keywords,) = [c for c in report.checks if c.name == "keywords"]
    assert keywords.ok is None and keywords.detail == NOT_RUN_KEYWORDS
    assert report.ok
    assert "--    keywords" in report.render()


async def test_a_fixture_without_keywords_has_no_keyword_check() -> None:
    report = await replay(builtin_thread("feature-request"))
    assert all(c.name != "keywords" for c in report.checks)


async def test_replay_uses_the_real_store_and_lifecycle(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "replay.db")
    try:
        report = await replay(builtin_thread("feature-request"), store=store)
        assert report.ok
        (proposal,) = await store.thread_proposals("C0EXAMPLE1", "1792310400.000100")
        assert proposal.state is ProposalState.PENDING
        runs = await store.thread_runs("C0EXAMPLE1", "1792310400.000100")
        assert [r.status.value for r in runs] == ["succeeded"]
    finally:
        await store.close()


def _bot_reply(data: dict[str, Any]) -> None:
    """Mark the reply as the bot's own message: ingest drops it, the file still expects it."""
    reply = next(e for e in data["events"] if e.get("thread_ts") and e["thread_ts"] != e["ts"])
    reply["bot_id"] = data["workspace"]["bot"]["bot_id"]


def _broken(name: str, change: Any) -> str:
    data = json.loads((PATHS[0].parent / f"{name}.json").read_text(encoding="utf-8"))
    change(data)
    return json.dumps(data)


async def test_a_mismatch_is_reported() -> None:
    thread = parse_thread(_broken("feature-request", _bot_reply), source="test")
    report = await replay(thread)
    assert not report.ok
    failed = {c.name for c in report.checks if c.ok is False}
    assert {"ingest accepted", "script"} <= failed
    assert "Does not match the expected outcome." in report.render()


async def test_a_refused_tool_call_stops_the_replay() -> None:
    thread = builtin_thread("mentions")

    async def refusing(self: Any, grant: Any, arguments: Any) -> Any:
        raise RuntimeError("nope")

    from jotflow.whiteboard.write_tools import WriteTools

    original = WriteTools.propose_issue
    WriteTools.propose_issue = refusing  # type: ignore[method-assign]
    try:
        report = await replay(thread)
    finally:
        WriteTools.propose_issue = original  # type: ignore[method-assign]
    (script,) = [c for c in report.checks if c.name == "script"]
    assert script.ok is False
    assert "propose_issue refused the scripted call" in script.detail


def test_the_report_never_contains_message_text() -> None:
    import asyncio

    thread = builtin_thread("bug-report")
    rendered = asyncio.run(replay(thread)).render()
    for message in thread.messages.values():
        if len(message.text) > 12:
            assert message.text not in rendered


def test_report_ok_ignores_checks_not_run() -> None:
    from jotflow.replay import Check

    report = ReplayReport("x", "y", [Check("a", True, ""), Check("b", None, "")])
    assert report.ok
    report.checks.append(Check("c", False, ""))
    assert not report.ok


# --- the command ---------------------------------------------------------------


def _args(**fields: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"thread": None, "list": False, "db": None}
    values.update(fields)
    return argparse.Namespace(**values)


def test_cli_replays_a_builtin(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(_args(thread="feature-request")) == command.EXIT_MATCH
    out = capsys.readouterr().out
    assert out.startswith("Replaying feature-request:")
    assert "Matches the expected outcome." in out


def test_cli_replays_a_file_and_keeps_a_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "replay.db"
    path = PATHS[0].parent / "chatter.json"
    assert command.run(_args(thread=str(path), db=db)) == command.EXIT_MATCH
    assert db.exists()


def test_cli_reports_a_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(_broken("feature-request", _bot_reply), encoding="utf-8")
    assert command.run(_args(thread=str(path))) == command.EXIT_MISMATCH


def test_cli_lists_the_fixtures(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(_args(list=True)) == command.EXIT_MATCH
    assert capsys.readouterr().out.split() == sorted(p.stem for p in PATHS)


@pytest.mark.parametrize("thread", [None, "no-such-thread", "missing.json"])
def test_cli_usage_problems(thread: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(_args(thread=thread)) == command.EXIT_USAGE
    assert capsys.readouterr().err


# --- internal paths ------------------------------------------------------------


def _state(thread_name: str = "feature-request") -> module._Replay:
    thread = builtin_thread(thread_name)
    store = MemoryStore()
    return module._Replay(thread, store, module._Clock(), {}, module._Poster())


def test_unknown_authors_and_topics() -> None:
    thread = builtin_thread("feature-request")
    source = module._FixtureSource(thread, [])
    assert source._name(None) == "unknown"
    assert source._name("U0NOBODY") == "U0NOBODY"
    with pytest.raises(module.ReplayError, match="before it exists"):
        _state().resolve("key:never")
    assert _state().resolve(None) is None


async def test_a_run_may_only_call_its_kinds_tools() -> None:
    state = _state()
    state.specs = {
        spec.name: spec
        for spec in module.write_tools(state.store, state.thread.policy, state.poster)
    }
    grant = Grant(run_id="r1", kind=RunKind.DRAFTING, channel_id="C0EXAMPLE1", thread_ts="1.1")
    with pytest.raises(module.ReplayError, match="may not call record_result"):
        await state.call(grant, "record_result", {})


async def test_an_existing_topic_without_an_issue_is_seeded_plainly() -> None:
    state = _state("existing-topic")
    plain_topic = state.thread.existing_topics[0].model_copy(
        update={"repo": None, "issue_number": None, "messages": []}
    )
    state.thread = state.thread.model_copy(update={"existing_topics": (plain_topic,)})
    await module._seed(state)
    record = await state.store.get_topic(state.topics[plain_topic.id])
    assert record is not None and record.topic.issue_number is None


async def test_a_run_that_does_not_start_is_an_error() -> None:
    state = _state()

    class Stuck:
        async def start_next(self, *, max_running: int) -> None:
            return None

    with pytest.raises(module.ReplayError, match="did not start"):
        await module._run_script(state, Stuck(), module.TokenRegistry(), [])  # type: ignore[arg-type]


async def test_missing_topics_and_chatter_are_reported() -> None:
    state = _state("chatter")
    checks = {c.name: c for c in await module._compare(state, [], {})}
    assert checks["chatter"].ok is False and "not marked chatter" in checks["chatter"].detail
    topical = _state("feature-request")
    checks = {c.name: c for c in await module._compare(topical, [], {})}
    assert checks["topics"].ok is False and "was not created" in checks["topics"].detail
