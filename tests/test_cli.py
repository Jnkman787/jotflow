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
def test_subcommands_dispatch(name: str) -> None:
    assert isinstance(main([name]), int)
