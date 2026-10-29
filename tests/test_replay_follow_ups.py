"""Replay of follow-ups (#304): partial packets, scripted revisions, and their checks."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jotflow import replay as module
from jotflow.replay import replay
from jotflow.testing.threads import ThreadError, builtin_paths, parse_thread

FOLDER = next(iter(builtin_paths())).parent
FIXTURES = (
    "follow-up-pending",
    "follow-up-filed-reply",
    "follow-up-filed-scope",
    "follow-up-unclear",
)


def data(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FOLDER / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def thread(fixture: dict[str, Any]) -> Any:
    return parse_thread(json.dumps(fixture), source="case.json")


def problem(fixture: dict[str, Any]) -> str:
    with pytest.raises(ThreadError) as exc:
        parse_thread(json.dumps(fixture), source="case.json")
    return str(exc.value)


@pytest.mark.parametrize("name", FIXTURES)
async def test_each_follow_up_fixture_replays(name: str) -> None:
    report = await replay(thread(data(name)))
    assert report.ok, report.render()
    assert "revisions" in {c.name for c in report.checks}


async def test_a_run_sees_only_messages_up_to_its_sees_until() -> None:
    prepared = await module.prepare(thread(data("follow-up-pending")))
    reader = prepared.state.reader
    assert reader is not None
    reader.until = "1792330000.000100"
    answer = await reader.replies("C0EXAMPLE1", "1792330000.000100", cursor=None, limit=50)
    assert [m["ts"] for m in answer["messages"]] == ["1792330000.000100"]
    reader.until = None
    answer = await reader.replies("C0EXAMPLE1", "1792330000.000100", cursor=None, limit=50)
    assert len(answer["messages"]) == 2


def test_sees_until_must_name_a_message_and_bound_the_calls() -> None:
    fixture = data("follow-up-pending")
    fixture["agent"][0]["sees_until"] = "1792339999.000100"
    assert "sees_until 1792339999.000100 is not a message in events" in problem(fixture)
    fixture = data("follow-up-pending")
    fixture["agent"][1]["sees_until"] = "1792330000.000100"
    assert "comes after its sees_until" in problem(fixture)


def test_a_revision_names_a_known_topic_and_known_messages() -> None:
    fixture = data("follow-up-pending")
    fixture["agent"][1]["calls"][1]["arguments"]["topic_id"] = "key:nothing"
    assert "topic key:nothing is neither existing nor declared" in problem(fixture)
    fixture = data("follow-up-pending")
    fixture["agent"][1]["calls"][1]["arguments"]["sources"] = [
        {"channel_id": "C0EXAMPLE1", "ts": "1792339999.000100"}
    ]
    assert "source 1792339999.000100 is not in events" in problem(fixture)
    fixture = data("follow-up-pending")
    fixture["agent"][1]["calls"][1]["arguments"]["changes"] = {"comment": "x"}
    assert "exactly one of issue or changes" in problem(fixture)


def test_expected_revisions_must_match_the_script() -> None:
    fixture = data("follow-up-pending")
    fixture["expected"]["proposals"][0]["revisions"] = 2
    assert "revisions 2, but the script revises key:csv-export 1 times" in problem(fixture)


async def test_a_wrong_reviser_or_field_fails_the_revision_check() -> None:
    fixture = data("follow-up-pending")
    fixture["expected"]["proposals"][0]["revised_by"] = ["U0CAROL"]
    fixture["expected"]["proposals"][0]["changed"] = ["title"]
    report = await replay(thread(fixture))
    check = next(c for c in report.checks if c.name == "revisions")
    assert not check.ok
    assert "last revision added ['U0BOB']" in check.detail
    assert "last revision changed ['body']" in check.detail


def test_a_scripted_revision_without_a_pending_draft_is_an_error() -> None:
    call = thread(data("follow-up-pending")).agent[1].calls[1]
    with pytest.raises(module.ReplayError, match="has no pending draft"):
        module.scripted_call(call, lambda topic: topic, [])
