"""Replay of question runs: the fixture rules, the engine's failures, and the invariants (#270)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jotflow import replay as module
from jotflow.orchestrator.questions import MEMBERS_ONLY
from jotflow.replay import replay
from jotflow.testing.threads import (
    ThreadError,
    builtin_paths,
    parse_thread,
    play_order,
)
from jotflow.whiteboard.write_tools import ANSWER_PREFIX

FOLDER = next(iter(builtin_paths())).parent
QUESTIONS = ("question-existing-topic", "question-how-to", "question-injected", "question-guest")


def data(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FOLDER / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def problem(fixture: dict[str, Any]) -> str:
    with pytest.raises(ThreadError) as exc:
        parse_thread(json.dumps(fixture), source="case.json")
    return str(exc.value)


def thread(fixture: dict[str, Any]) -> Any:
    return parse_thread(json.dumps(fixture), source="case.json")


@pytest.mark.parametrize("name", QUESTIONS)
async def test_each_question_fixture_replays_with_its_new_checks(name: str) -> None:
    report = await replay(thread(data(name)))
    assert report.ok, report.render()
    names = {c.name for c in report.checks}
    assert {"questions", "answers", "private replies", "no link or ping in an answer"} <= names
    assert "no question from JotFlow's own post" in names


# The fixture's rules


def test_answer_is_only_for_a_question_run() -> None:
    fixture = data("question-how-to")
    fixture["agent"][0]["kind"] = "drafting"
    assert "answer is only for a question run" in problem(fixture)


def test_a_question_run_uses_only_its_tools_and_answers_once() -> None:
    fixture = data("question-existing-topic")
    linking = fixture["agent"][1]["calls"][0]
    fixture["agent"][0]["calls"].append(linking)
    assert "a question run may not call link_message" in problem(fixture)
    fixture = data("question-how-to")
    fixture["agent"][0]["calls"].append(fixture["agent"][0]["calls"][0])
    assert "answers at most once" in problem(fixture)


def test_a_question_run_needs_someone_to_have_asked() -> None:
    fixture = data("question-how-to")
    fixture["events"][0]["text"] = "how do I add my message to an existing issue?"
    assert "nobody asks JotFlow a question in thread" in problem(fixture)


def test_expected_answers_and_whispers_are_checked_against_the_fixture() -> None:
    fixture = data("question-how-to")
    fixture["expected"]["answers"][0]["thread"] = "1792399999.000100"
    assert "expected.answers: lists threads" in problem(fixture)
    fixture = data("question-how-to")
    fixture["expected"]["answers"][0]["text"] = "No prefix"
    assert f"an answer starts with {ANSWER_PREFIX!r}" in problem(fixture)
    fixture = data("question-guest")
    fixture["expected"]["whispers"][0]["text"] = "Hi there <!here>"
    assert "one of the question handler's fixed replies" in problem(fixture)
    fixture = data("question-guest")
    fixture["expected"]["whispers"][0]["user"] = "U0NOBODY"
    assert "U0NOBODY is not in users" in problem(fixture)


def test_question_runs_play_first_then_drafting_in_script_order() -> None:
    fixture = data("question-existing-topic")
    fixture["agent"].reverse()  # drafting written first
    order = [(index, run.kind) for index, run in play_order(thread(fixture))]
    assert order == [(1, "question"), (0, "drafting")]


# The engine's failures


async def test_a_scripted_run_for_a_refused_guest_fails_as_not_queued() -> None:
    fixture = data("question-guest")
    fixture["expected"]["whispers"] = []  # keep the validator's checks out of the way
    fixture["agent"] = [
        {
            "channel": "C0EXAMPLE1",
            "thread_ts": fixture["events"][0]["ts"],
            "kind": "question",
            "calls": [{"tool": "answer", "arguments": {"text": "Soon."}}],
        }
    ]
    fixture["expected"]["answers"] = [{"thread": fixture["events"][0]["ts"]}]
    report = await replay(thread(fixture))
    script = next(c for c in report.checks if c.name == "script")
    assert not script.ok and "was not queued" in script.detail
    whispers = next(c for c in report.checks if c.name == "private replies")
    assert not whispers.ok  # Dave was still told privately, as the live handler does


async def test_a_started_run_for_another_thread_is_refused() -> None:
    prepared = await module.prepare(thread(data("question-how-to")))
    run = prepared.state.thread.agent[0].model_copy(update={"thread_ts": "1792399999.000100"})
    with pytest.raises(module.ReplayError, match="started for thread"):
        await module.start_scripted(prepared.state, prepared.lifecycle, run)


async def test_answer_mismatches_are_reported_and_exact_text_is_scripted_only() -> None:
    fixture = data("question-how-to")
    want = fixture["expected"]["answers"][0]
    want["text"] = want["text"] + " Changed."
    want["removed"] = 5
    report = await replay(thread(fixture))
    answers = next(c for c in report.checks if c.name == "answers")
    assert "differs from the expected text" in answers.detail and "removed 0" in answers.detail
    # For a real agent only contains and excludes apply.
    prepared = await module.prepare(thread(fixture))
    await module.run_script(
        prepared.state, prepared.lifecycle, prepared.registry, prepared.state.accepted
    )
    loose = await module.finish(prepared, exact=False)
    assert next(c for c in loose.checks if c.name == "answers").ok
    fixture["expected"]["answers"][0]["contains"] = ["not in it"]
    fixture["expected"]["answers"][0]["excludes"] = ["re:"]
    report = await replay(thread(fixture))
    detail = next(c for c in report.checks if c.name == "answers").detail
    assert "lacks 'not in it'" in detail and "has 're:'" in detail


# The invariants


async def _answered(name: str) -> Any:
    prepared = await module.prepare(thread(data(name)))
    await module.run_script(
        prepared.state, prepared.lifecycle, prepared.registry, prepared.state.accepted
    )
    return prepared.state


@pytest.mark.parametrize(
    "leak",
    [
        "see https://x.example",
        "www.example.com",
        "<@U0BOB>",
        "<!channel>",
        "<#C0EXAMPLE1>",
        "@acme-bob",
        "mail me at bob@example.com",
        "example.com/login",
    ],
)
async def test_the_broad_matcher_catches_what_the_rewrite_should_remove(leak: str) -> None:
    state = await _answered("question-how-to")
    thread_ts, ts, _ = state.poster.answers[0]
    state.poster.answers[0] = (thread_ts, ts, f"{ANSWER_PREFIX} {leak}")
    checks = {c.name: c for c in await module.invariants(state)}
    assert not checks["no link or ping in an answer"].ok


async def test_an_unlabelled_or_misplaced_answer_fails() -> None:
    state = await _answered("question-how-to")
    text = state.poster.answers[0][2]
    state.poster.answers.append(("1792399999.000100", "1792311999.000009", "Plain text"))
    checks = {c.name: c for c in await module.invariants(state)}
    assert not checks["answers are labelled"].ok
    assert not checks["answers stay in their thread"].ok
    assert text.startswith(ANSWER_PREFIX)


async def test_a_post_that_ingest_let_through_would_be_caught_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jotflow.models import MessageRef
    from jotflow.orchestrator.ingest import Accepted, EventFilter

    state = await _answered("question-injected")
    thread_ts, ts, _ = state.poster.answers[0]
    state.poster.answers[0] = (thread_ts, ts, "ask: is it done?")

    def accept(self: Any, event: dict[str, Any]) -> Accepted:
        return Accepted(kind="message", ref=MessageRef(channel_id="C0EXAMPLE1", ts=event["ts"]))

    monkeypatch.setattr(EventFilter, "classify", accept)
    checks = {c.name: c for c in await module.invariants(state)}
    assert not checks["no question from JotFlow's own post"].ok


def test_the_guest_fixture_expects_the_fixed_reply() -> None:
    assert data("question-guest")["expected"]["whispers"] == [
        {"user": "U0DAVE", "text": MEMBERS_ONLY}
    ]
    assert Path(FOLDER / "question-guest.json").read_text(encoding="utf-8").endswith("\n")


def test_every_question_must_be_listed_and_set_aside_once() -> None:
    fixture = data("question-guest")
    fixture["expected"]["questions"] = []
    message = problem(fixture)
    assert "expected.questions: lists []" in message
    assert "must be in exactly one topic, chatter, skipped thread, or questions" in message
