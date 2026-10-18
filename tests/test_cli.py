import os
import pathlib

import pytest

from jotflow import __version__
from jotflow.cli import COMMANDS, main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_subcommands_dispatch(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Keep a developer's real .env, jotflow.toml, and tokens out of this test,
    # so `run` stops at configuration instead of connecting to Slack.
    monkeypatch.chdir(tmp_path)
    for var in list(os.environ):
        if var.startswith("JOTFLOW_"):
            monkeypatch.delenv(var)
    assert isinstance(main([name]), int)
