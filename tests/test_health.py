"""The Slack connection's health signal: log lines, heartbeat, and `jotflow health` (#133)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import stat
from pathlib import Path

import pytest

from jotflow.cli import main
from jotflow.commands import health as health_command
from jotflow.commands import run as run_command
from jotflow.orchestrator.health import (
    ConnectionWatch,
    Heartbeat,
    heartbeat_age,
    heartbeat_path,
)


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Link:
    """A Socket Mode connection whose state the test sets."""

    def __init__(self) -> None:
        self.up = True
        self.fail = False

    async def is_connected(self) -> bool:
        if self.fail:
            raise RuntimeError("socket gone")
        return self.up


def test_the_heartbeat_path_comes_from_the_environment_or_tmp(tmp_path: Path) -> None:
    assert heartbeat_path({"JOTFLOW_HEARTBEAT_FILE": str(tmp_path / "hb")}) == tmp_path / "hb"
    assert heartbeat_path({}).name == "jotflow-heartbeat"


def test_a_beat_is_a_private_time_and_its_age_is_read_back(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    Heartbeat(path, clock=lambda: 500.0).beat()
    assert path.read_text(encoding="ascii") == "500\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert heartbeat_age(path, 530.0) == 30.0
    assert heartbeat_age(path, 400.0) == 0.0  # a clock that stepped back is not negative
    assert list(tmp_path.iterdir()) == [path]  # no temp file left behind


@pytest.mark.parametrize("content", [None, "", "not a time\n", b"\xff\xfe"])
def test_a_missing_or_unreadable_heartbeat_has_no_age(
    tmp_path: Path, content: str | bytes | None
) -> None:
    path = tmp_path / "hb"
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content, encoding="ascii")
    assert heartbeat_age(path, 1.0) is None


async def test_a_drop_and_recovery_log_three_plain_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock, link, path = Clock(), Link(), tmp_path / "hb"
    watch = ConnectionWatch(link, Heartbeat(path, clock=clock), clock=clock)
    with caplog.at_level(logging.INFO, logger="jotflow.health"):
        await watch.check()  # connected: beats
        assert path.exists()
        path.unlink()
        link.up = False
        clock.now += 5
        await watch.check()
        clock.now += 5
        await watch.check()  # still down, under 30 s: no new line
        clock.now += 30
        await watch.check()
        assert not path.exists()  # no beat while down
        link.up = True
        clock.now += 7
        await watch.check()
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        "socket mode disconnected",
        "socket mode reconnecting (attempt 1)",
        "socket mode reconnecting (attempt 2)",
        "socket mode reconnected after 42s",  # from the first check that saw it down
    ]
    assert path.exists()  # the first beat after reconnecting is immediate


async def test_beats_are_every_thirty_seconds_while_connected(tmp_path: Path) -> None:
    clock, path = Clock(), tmp_path / "hb"
    watch = ConnectionWatch(Link(), Heartbeat(path, clock=clock), clock=clock)
    await watch.check()
    first = path.read_text(encoding="ascii")
    clock.now += 10
    await watch.check()
    assert path.read_text(encoding="ascii") == first
    clock.now += 25
    await watch.check()
    assert path.read_text(encoding="ascii") != first


async def test_a_failing_check_counts_as_disconnected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    link = Link()
    link.fail = True
    watch = ConnectionWatch(link, Heartbeat(tmp_path / "hb"), clock=Clock())
    with caplog.at_level(logging.INFO, logger="jotflow.health"):
        await watch.check()
    assert "socket mode disconnected" in caplog.text


async def test_an_unwritable_heartbeat_is_a_warning_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="ascii")
    watch = ConnectionWatch(Link(), Heartbeat(blocked / "hb"), clock=Clock())
    with caplog.at_level(logging.WARNING, logger="jotflow.health"):
        await watch.check()
    assert "could not write the heartbeat file" in caplog.text
    assert str(tmp_path) not in caplog.text


async def test_run_checks_until_stopped(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    watch = ConnectionWatch(Link(), Heartbeat(path), check_seconds=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watch.run(stop))
    for _ in range(100):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert path.exists()


# jotflow health


def health(monkeypatch: pytest.MonkeyPatch, path: Path, *args: str) -> int:
    monkeypatch.setenv("JOTFLOW_HEARTBEAT_FILE", str(path))
    for name in ("JOTFLOW_SLACK_BOT_TOKEN", "JOTFLOW_SLACK_APP_TOKEN"):
        monkeypatch.delenv(name, raising=False)  # no tokens needed
    return main(["health", *args])


def test_health_is_zero_while_the_heartbeat_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    path = tmp_path / "hb"
    Heartbeat(path, clock=lambda: time.time() - 20).beat()
    assert health(monkeypatch, path) == 0
    assert capsys.readouterr().out.startswith("healthy: connected to Slack, heartbeat 20s ago")


def test_health_is_one_when_stale_missing_or_garbled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    path = tmp_path / "hb"
    assert health(monkeypatch, path) == 1
    assert "no heartbeat at" in capsys.readouterr().out
    Heartbeat(path, clock=lambda: time.time() - 120).beat()
    assert health(monkeypatch, path) == 1
    assert "the last heartbeat was 120s ago" in capsys.readouterr().out
    assert health(monkeypatch, path, "--max-age", "300") == 0
    path.write_text("garbage", encoding="ascii")
    assert health(monkeypatch, path) == 1


def test_health_reads_no_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env, no jotflow.toml here
    (tmp_path / ".env").write_text("JOTFLOW_SLACK_BOT_TOKEN=broken", encoding="utf-8")
    assert health(monkeypatch, tmp_path / "hb") == 1
    assert health_command.run(argparse.Namespace()) == 1  # defaults apply


# jotflow run keeps the heartbeat while connected, and removes it on a clean stop


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.client = Link()

    async def connect_async(self) -> None:
        self.calls.append("connect")

    async def close_async(self) -> None:
        self.calls.append("close")


async def test_run_until_stopped_beats_while_connected_then_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hb"
    monkeypatch.setenv("JOTFLOW_HEARTBEAT_FILE", str(path))
    connection = FakeConnection()
    stop = asyncio.Event()
    task = asyncio.create_task(run_command.run_until_stopped(connection, stop))
    for _ in range(200):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    assert path.exists()
    stop.set()
    await task
    assert connection.calls == ["connect", "close"] and not path.exists()


async def test_a_connection_without_a_state_is_not_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hb"
    monkeypatch.setenv("JOTFLOW_HEARTBEAT_FILE", str(path))

    class Bare:
        async def connect_async(self) -> None: ...

        async def close_async(self) -> None: ...

    stop = asyncio.Event()
    stop.set()
    await run_command.run_until_stopped(Bare(), stop)
    assert not path.exists()
