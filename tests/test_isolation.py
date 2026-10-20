"""Per-run uids (#181): what can be checked without root. The real drop, cross-reads
and sweep run in the agent image, in CI's cross-container job."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jotflow.adapters import isolation
from jotflow.adapters.isolation import (
    EXPECTED_CAPS,
    IsolationError,
    RunUser,
    UidPool,
    capabilities,
    check_child,
    check_startup,
)


def status(uid: int = 0, cap_eff: int = EXPECTED_CAPS) -> str:
    return (
        f"Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t0\t0\t0\t0\n"
        f"CapInh:\t0000000000000000\nCapPrm:\t{cap_eff:016x}\nCapEff:\t{cap_eff:016x}\n"
    )


def test_status_files_are_read_for_uids_and_effective_caps() -> None:
    assert capabilities(status(0, 0xC0)) == ([0, 0, 0, 0], 0xC0)
    assert capabilities("nothing here") == ([], -1)


@pytest.mark.parametrize(
    "given",
    [
        status(uid=10002, cap_eff=0),  # the default non-root runner
        status(uid=10002, cap_eff=EXPECTED_CAPS),  # cap_add on a non-root user does nothing
        status(uid=0, cap_eff=0),  # root, but with no capabilities
        status(uid=0, cap_eff=EXPECTED_CAPS | 1 << 21),  # one too many: SYS_ADMIN
        status(uid=0, cap_eff=0x00000000A80425FB),  # Docker's default set
    ],
)
def test_uid_mode_refuses_any_runner_but_root_with_exactly_setuid_and_setgid(given: str) -> None:
    with pytest.raises(IsolationError, match=r"docker-compose\.isolation\.yml"):
        check_startup(preset="command", env_allow=(), environ={}, status=given)


@pytest.mark.parametrize(
    ("preset", "allow", "environ", "ok"),
    [
        ("claude-code", ("ANTHROPIC_API_KEY",), {"ANTHROPIC_API_KEY": "sk-ant-x"}, True),
        ("claude-code", (), {"ANTHROPIC_API_KEY": "sk-ant-x"}, False),  # not passed on
        ("claude-code", ("ANTHROPIC_API_KEY",), {"ANTHROPIC_API_KEY": "  "}, False),
        ("claude-code", ("ANTHROPIC_API_KEY",), {}, False),
        ("codex", ("OPENAI_API_KEY",), {"OPENAI_API_KEY": "sk-x"}, True),
        ("codex", ("CODEX_API_KEY",), {"CODEX_API_KEY": "sk-x"}, True),
        ("codex", ("ANTHROPIC_API_KEY",), {"ANTHROPIC_API_KEY": "sk-ant-x"}, False),
        ("command", (), {}, True),  # a team's own command: its login is its own business
    ],
)
def test_uid_mode_needs_the_presets_api_key(
    preset: str, allow: tuple[str, ...], environ: dict[str, str], ok: bool
) -> None:
    if ok:
        check_startup(preset=preset, env_allow=allow, environ=environ, status=status())
    else:
        with pytest.raises(IsolationError, match="own home"):
            check_startup(preset=preset, env_allow=allow, environ=environ, status=status())


def test_the_pool_goes_round_robin_and_never_hands_out_a_busy_uid() -> None:
    pool = UidPool(first=20001, size=3)
    a, b = pool.acquire(), pool.acquire()
    assert (a, b) == (RunUser(20001, 20001), RunUser(20002, 20002))
    assert a is not None
    pool.release(a, clean=True)
    # Round robin: the next free one is 20003, not the one just freed.
    assert pool.acquire() == RunUser(20003, 20003)
    assert pool.acquire() == RunUser(20001, 20001)
    assert pool.acquire() is None  # all three busy


def test_a_uid_that_didnt_sweep_clean_is_never_reused() -> None:
    pool = UidPool(first=20001, size=2)
    a = pool.acquire()
    assert a is not None
    pool.release(a, clean=False)
    assert pool.acquire() == RunUser(20002, 20002)
    assert pool.acquire() is None


def test_the_image_pool_matches_the_runner(tmp_path: Path) -> None:
    dockerfile = (Path(__file__).parents[1] / "docker" / "Dockerfile.agent").read_text()
    first, last = isolation.POOL_FIRST, isolation.POOL_FIRST + isolation.POOL_SIZE - 1
    assert f"seq {first} {last}" in dockerfile


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        (status(uid=20001, cap_eff=0), True),
        (status(uid=20002, cap_eff=0), False),
        (status(uid=20001, cap_eff=EXPECTED_CAPS), False),
        (status(uid=0, cap_eff=0), False),
    ],
)
def test_a_started_child_must_be_the_runs_uid_with_no_capabilities(
    tmp_path: Path, text: str, ok: bool
) -> None:
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "status").write_text(text)
    assert check_child(42, RunUser(20001, 20001), proc=tmp_path) is ok


def test_a_child_that_already_exited_left_nothing_to_check(tmp_path: Path) -> None:
    assert check_child(99, RunUser(20001, 20001), proc=tmp_path)


def test_none_mode_does_nothing_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from jotflow.commands import runner as command

    called: list[Any] = []
    monkeypatch.setattr(isolation.os, "setgroups", lambda gids: called.append(gids))
    config: Any = type("C", (), {"isolation": "none"})()
    command.start_isolation(config, {}, "anything")
    assert called == []


def test_uid_mode_joins_every_run_group_after_its_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from jotflow.commands import runner as command

    called: list[list[int]] = []
    monkeypatch.setattr(isolation.os, "setgroups", lambda gids: called.append(list(gids)))

    class Agent:
        name = "command"

    config: Any = type("C", (), {"isolation": "uid", "agent": Agent(), "env_allow": ()})()
    command.start_isolation(config, {}, status())
    assert called == [list(range(20001, 20065))]
    with pytest.raises(IsolationError):
        command.start_isolation(config, {}, status(uid=10002, cap_eff=0))


def test_child_env_gives_a_run_its_own_home_and_scratch_space(tmp_path: Path) -> None:
    from jotflow.adapters.runner import child_env
    from jotflow.models import RunKind
    from tests.test_runner import ENVIRON, config

    cfg = config(tmp_path)
    shared = child_env(cfg, RunKind.DRAFTING, "run-1", tmp_path, ENVIRON)
    own = child_env(cfg, RunKind.DRAFTING, "run-1", tmp_path, ENVIRON, home=tmp_path / "home")
    assert shared["HOME"] == ENVIRON.get("HOME", str(tmp_path))
    assert own["HOME"] == str(tmp_path / "home")
    assert own["TMPDIR"] == str(tmp_path / "tmp")


async def test_a_full_pool_refuses_a_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow.adapters.runner import LaunchRefused, LaunchRequest, Runner
    from jotflow.models import RunKind
    from tests.test_runner import ENVIRON, TOKEN, config

    runner = Runner(config(tmp_path, isolation="uid"), environ=ENVIRON)
    monkeypatch.setattr(runner._pool, "acquire", lambda: None)
    with pytest.raises(LaunchRefused) as refused:
        await runner.launch(LaunchRequest(run_id="run-1", kind=RunKind.DRAFTING, token=TOKEN))
    assert (refused.value.status, refused.value.reason) == (429, "the runner has no free run uid")


def test_the_startup_line_says_each_run_has_its_own_uid(tmp_path: Path) -> None:
    from jotflow.commands.runner import startup_line
    from tests.test_runner import config

    assert startup_line(config(tmp_path, isolation="uid")).endswith(", each run as its own uid")
    assert "own uid" not in startup_line(config(tmp_path))
