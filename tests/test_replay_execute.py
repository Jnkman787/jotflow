"""replay --execute (#31): approved proposals reach the fake GitHub exactly as planned."""

from __future__ import annotations

import argparse

import pytest

from jotflow.commands import replay as replay_command
from jotflow.replay_agent import AgentOptions
from jotflow.replay_execute import StandInPreset, replay_execution
from jotflow.testing.threads import builtin_paths, builtin_thread


@pytest.mark.parametrize("name", sorted(p.stem for p in builtin_paths()))
async def test_the_stand_in_executes_every_fixture_exactly(name: str) -> None:
    report = await replay_execution(
        builtin_thread(name), AgentOptions("scripted", name, time_limit=60)
    )
    assert report.ok, report.render()
    assert [c.name for c in report.checks] == [
        "execute (first attempt)",
        "execute (after an interrupted attempt)",
    ]


async def test_an_agent_that_changes_the_text_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    from jotflow import replay_execute

    def tampering(options: AgentOptions, *, crash: bool = False) -> StandInPreset:
        return StandInPreset(crash_after_write=crash, tamper=not crash)

    monkeypatch.setattr(replay_execute, "_executor", tampering)
    report = await replay_execution(
        builtin_thread("feature-request"), AgentOptions("scripted", "x", time_limit=60)
    )
    first = report.checks[0]
    assert first.ok is False and "differ from the planned ones" in first.detail


async def test_an_agent_that_writes_again_after_an_interruption_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jotflow import replay_execute

    def forgetful(options: AgentOptions, *, crash: bool = False) -> StandInPreset:
        return StandInPreset(crash_after_write=crash, skip_marker_check=not crash)

    monkeypatch.setattr(replay_execute, "_executor", forgetful)
    report = await replay_execution(
        builtin_thread("feature-request"), AgentOptions("scripted", "x", time_limit=60)
    )
    first, second = report.checks
    assert first.ok is True  # nothing to find the first time
    assert second.ok is False and "wrote again after an interrupted attempt" in second.detail


async def test_a_missing_label_fails_the_run_and_writes_nothing() -> None:
    report = await replay_execution(
        builtin_thread("missing-label"), AgentOptions("scripted", "missing-label", time_limit=60)
    )
    assert report.ok, report.render()


async def test_an_agent_that_does_not_exit_cleanly_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from jotflow import replay_execute
    from jotflow.adapters.presets import CommandPreset

    monkeypatch.setattr(
        replay_execute,
        "_executor",
        lambda options, *, crash=False: CommandPreset((sys.executable, "-c", "exit(4)")),
    )
    report = await replay_execution(
        builtin_thread("existing-topic"), AgentOptions("scripted", "x", time_limit=30)
    )
    first, second = report.checks
    assert first.ok is False and "exited exit 4" in first.detail and "not applied" in first.detail
    assert second.ok is False and "wrote nothing" in second.detail


async def test_an_unknown_agent_is_a_failed_check() -> None:
    report = await replay_execution(builtin_thread("feature-request"), AgentOptions("gpt", "x"))
    assert report.checks[0].ok is False and "choose one of" in report.checks[0].detail


def args(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "thread": None,
        "list": False,
        "db": None,
        "agent": None,
        "all": False,
        "repeat": 1,
        "model": None,
        "time_limit": 60.0,
        "yes": False,
        "execute": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_execute_needs_an_agent(capsys: pytest.CaptureFixture[str]) -> None:
    assert replay_command.run(args(thread="chatter", execute=True)) == replay_command.EXIT_USAGE
    assert "--execute needs --agent" in capsys.readouterr().err


def test_execute_estimates_execution_runs_and_needs_yes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = replay_command.run(args(all=True, agent="claude-code", execute=True))
    assert code == replay_command.EXIT_USAGE
    proposals = sum(len(builtin_thread(p.stem).expected.proposals) for p in builtin_paths())
    assert f"{proposals * 2} claude-code execution run(s) on haiku" in capsys.readouterr().err


def test_execute_with_the_stand_in_from_the_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        replay_command.run(args(thread="keywords", agent="scripted", execute=True, model="haiku"))
        == replay_command.EXIT_MATCH
    )
    assert "3 proposal(s) handled as planned" in capsys.readouterr().out


def test_the_expected_body_keeps_the_creating_marker() -> None:
    from jotflow.execution.writes import PlannedWrite, marker
    from jotflow.replay_execute import _expected

    repos = dict(builtin_thread("body-update").github)
    write = PlannedWrite(
        tool="issue_write",
        arguments={
            "method": "update",
            "owner": "acme-co",
            "repo": "web",
            "issue_number": 42,
            "body": f"New.\n\n---\n{marker('a1b2c3d4e5f60718')}",
        },
        markers_from_current=True,
    )
    body = _expected(write, repos, "acme-co/web")["body"]
    assert body == (f"New.\n\n---\n{marker('a1b2c3d4e5f60718')}\n{marker('0123456789abcdef')}")
