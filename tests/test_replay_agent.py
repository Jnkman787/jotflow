"""replay --agent (#23): fixtures run end to end through the runner and real MCP."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from jotflow.adapters.presets import CommandPreset
from jotflow.commands import replay as replay_command
from jotflow.models import RunKind
from jotflow.replay import ReplayError
from jotflow.replay_agent import (
    AGENTS,
    AgentOptions,
    Tally,
    _preset,
    repeat,
    replay_with_agent,
)
from jotflow.testing.threads import builtin_paths, builtin_thread


def failing(options: AgentOptions, index: int) -> CommandPreset:
    return CommandPreset((sys.executable, "-c", "exit(4)"))


@pytest.mark.parametrize("name", sorted(p.stem for p in builtin_paths()))
async def test_the_scripted_agent_matches_every_fixture_over_real_mcp(name: str) -> None:
    report = await replay_with_agent(
        builtin_thread(name), AgentOptions("scripted", name, time_limit=60)
    )
    assert report.ok, report.render()
    names = [check.name for check in report.checks]
    assert names[0] == "agent scripted"
    assert {"no proposal from chatter", "assignees are linked"} <= set(names)


def test_presets_and_the_stand_in_are_the_agents() -> None:
    assert AGENTS == ("scripted", "claude-code")
    stand_in = _preset(AgentOptions("scripted", "chatter"), 1)
    assert isinstance(stand_in, CommandPreset)
    assert stand_in.command[-4:] == ("chatter", "--run", "1", "{mcp_config}")
    with pytest.raises(ReplayError, match="choose one of"):
        _preset(AgentOptions("gpt", "chatter"), 0)


async def test_an_agent_that_fails_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from jotflow import replay_agent

    monkeypatch.setattr(
        replay_agent,
        "_preset",
        failing,
    )
    report = await replay_with_agent(
        builtin_thread("feature-request"),
        AgentOptions("scripted", "feature-request", time_limit=30),
    )
    assert not report.ok
    agent = report.checks[0]
    assert agent.ok is False and "exited exit 4" in agent.detail
    topics = next(c for c in report.checks if c.name == "topics")
    assert topics.ok is False  # nothing was linked, and the report says so


async def test_an_unknown_agent_is_a_failed_check() -> None:
    report = await replay_with_agent(builtin_thread("chatter"), AgentOptions("gpt", "chatter"))
    assert report.checks[0].ok is False and "choose one of" in report.checks[0].detail


async def test_repeat_tallies_each_check() -> None:
    tally, reports = await repeat(
        builtin_thread("chatter"), AgentOptions("scripted", "chatter", time_limit=60), 2
    )
    assert (tally.runs, tally.passed, len(reports)) == (2, 2, 2)
    assert tally.render() == "chatter: 2/2 runs matched"
    partial = Tally("x", runs=2, passed=1, checks={"topics": 1, "asks": 2})
    assert partial.render() == "x: 1/2 runs matched\n  topics: 1/2"


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
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("kw", "problem"),
    [
        ({"thread": "chatter", "repeat": 2}, "--repeat needs --agent"),
        ({"thread": "chatter", "agent": "scripted", "repeat": 0}, "--repeat needs --agent"),
        ({"thread": "chatter", "agent": "scripted", "db": Path("x.db")}, "cannot be combined"),
        ({"thread": "chatter", "all": True}, "not both"),
    ],
)
def test_replay_option_problems_exit_2(
    kw: dict[str, object], problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert replay_command.run(args(**kw)) == replay_command.EXIT_USAGE
    assert problem in capsys.readouterr().err


def test_replay_all_scripted_and_with_the_stand_in(capsys: pytest.CaptureFixture[str]) -> None:
    assert replay_command.run(args(all=True)) == replay_command.EXIT_MATCH
    assert capsys.readouterr().out.count("Matches the expected outcome.") == len(
        list(builtin_paths())
    )
    assert replay_command.run(args(thread="chatter", agent="scripted")) == replay_command.EXIT_MATCH
    assert "agent scripted" in capsys.readouterr().out
    assert (
        replay_command.run(args(thread="mentions", agent="scripted", repeat=2, model="haiku"))
        == replay_command.EXIT_MATCH
    )
    assert "mentions: 2/2 runs matched" in capsys.readouterr().out


def test_a_failed_repeat_prints_the_failing_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jotflow import replay_agent

    monkeypatch.setattr(
        replay_agent,
        "_preset",
        failing,
    )
    code = replay_command.run(args(thread="feature-request", agent="scripted", repeat=2))
    assert code == replay_command.EXIT_MISMATCH
    out = capsys.readouterr().out
    assert "Does not match" in out and "feature-request: 0/2 runs matched" in out


def test_the_model_reaches_drafting_runs_only() -> None:
    options = AgentOptions("claude-code", "x", models={RunKind.DRAFTING: "haiku"})
    assert options.models == {RunKind.DRAFTING: "haiku"}
