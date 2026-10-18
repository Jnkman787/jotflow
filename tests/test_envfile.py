from __future__ import annotations

import stat
from pathlib import Path

import pytest

from jotflow.envfile import parse_env, read_env, update_env_text, write_env


def test_parse_env() -> None:
    text = "# comment\nA=1\nexport B = two \nC='quoted value'\nD=\"x\"\n\nnot a line\nA=3\n"
    assert parse_env(text) == {"A": "3", "B": "two", "C": "quoted value", "D": "x"}


def test_commented_keys_are_ignored() -> None:
    assert parse_env("# A=1\n  #B=2\n") == {}


def test_update_replaces_in_place_and_keeps_everything_else() -> None:
    text = "# Slack\nA=old\nOTHER=keep\n# trailing\n"
    assert update_env_text(text, {"A": "new"}) == "# Slack\nA=new\nOTHER=keep\n# trailing\n"


def test_update_appends_new_keys_after_a_blank_line() -> None:
    assert update_env_text("OTHER=1\n", {"A": "x", "B": "y"}) == "OTHER=1\n\nA=x\nB=y\n"


def test_update_on_empty_text() -> None:
    assert update_env_text("", {"A": "x"}) == "A=x\n"


def test_update_collapses_duplicate_definitions() -> None:
    assert update_env_text("A=1\nB=2\nA=3\n", {"A": "new"}) == "A=new\nB=2\n"


def test_update_leaves_commented_key_alone() -> None:
    assert update_env_text("# A=example\n", {"A": "x"}) == "# A=example\n\nA=x\n"


def test_update_is_idempotent() -> None:
    once = update_env_text("X=1\n", {"A": "x"})
    assert update_env_text(once, {"A": "x"}) == once


@pytest.mark.parametrize(
    ("key", "value"),
    [("BAD KEY", "x"), ("1A", "x"), ("A", "line\nbreak"), ("A", "nul\0")],
)
def test_update_rejects_unsafe_input(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        update_env_text("", {key: value})


def test_write_env_creates_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env(path, {"A": "secret"})
    assert path.read_text() == "A=secret\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_write_env_tightens_existing_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("OTHER=1\n")
    path.chmod(0o644)
    write_env(path, {"A": "x"})
    assert read_env(path) == {"OTHER": "1", "A": "x"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_env_leaves_file_untouched_on_bad_input(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    with pytest.raises(ValueError):
        write_env(path, {"A": "bad\nvalue"})
    assert path.read_text() == "A=1\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_read_env_missing_file(tmp_path: Path) -> None:
    assert read_env(tmp_path / "missing") == {}


def test_update_removes_a_key_set_to_none() -> None:
    text = "# keep\nA=1\nB=2\nB=3\n"
    assert update_env_text(text, {"B": None, "C": None}) == "# keep\nA=1\n"


def test_write_env_starts_a_new_file_from_the_template(tmp_path: Path) -> None:
    path = tmp_path / "agent.env"
    write_env(path, {"A": "1"}, template="# what A is\nA=\nB=\n")
    assert path.read_text(encoding="utf-8") == "# what A is\nA=1\nB=\n"
    write_env(path, {"B": "2"}, template="ignored once the file exists\n")
    assert path.read_text(encoding="utf-8") == "# what A is\nA=1\nB=2\n"


def test_write_envs_puts_back_what_it_replaced_when_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow import envfile
    from jotflow.envfile import write_envs

    first, second, new = tmp_path / "a.env", tmp_path / "b.env", tmp_path / "c.env"
    first.write_text("A=old\n")
    second.write_text("B=old\n")
    real_replace = envfile.os.replace

    def failing(src: str, dst: object) -> None:
        if Path(str(dst)).name == "b.env":
            raise OSError("disk full")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(envfile.os, "replace", failing)
    with pytest.raises(OSError):
        write_envs([(first, {"A": "new"}, ""), (new, {"C": "1"}, ""), (second, {"B": "new"}, "")])
    assert first.read_text() == "A=old\n" and second.read_text() == "B=old\n"
    assert not new.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.env", "b.env"]


def test_write_envs_with_bad_input_changes_nothing(tmp_path: Path) -> None:
    from jotflow.envfile import write_envs

    first = tmp_path / "a.env"
    first.write_text("A=old\n")
    with pytest.raises(ValueError):
        write_envs([(first, {"A": "new"}, ""), (tmp_path / "b.env", {"B": "two\nlines"}, "")])
    assert first.read_text() == "A=old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.env"]
