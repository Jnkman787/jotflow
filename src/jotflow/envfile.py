"""Read and update a ``.env`` file without disturbing what else is in it.

``jotflow init`` writes the Slack tokens here. Comments, blank lines, and keys
JotFlow does not own are kept as they are. The file is written atomically and
is readable only by its owner, because it holds secrets.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*")

FILE_MODE = 0o600


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_env(text: str) -> dict[str, str]:
    """Return the ``KEY=value`` pairs in ``text``. Later keys win."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LINE.fullmatch(line)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def read_env(path: Path) -> dict[str, str]:
    """Read ``path``, or return an empty mapping if it does not exist."""
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _check(key: str, value: str) -> None:
    if not _KEY.fullmatch(key):
        raise ValueError(f"not a valid environment variable name: {key!r}")
    if any(ch in value for ch in "\r\n\0"):
        raise ValueError(f"value for {key} must be a single line")


def update_env_text(text: str, updates: Mapping[str, str | None]) -> str:
    """Return ``text`` with each key in ``updates`` set, replacing in place or appending.

    A ``None`` value removes the key.
    """
    for key, value in updates.items():
        _check(key, value or "")
    pending = {key: value for key, value in updates.items() if value is not None}
    lines: list[str] = []
    for line in text.splitlines():
        match = None if line.lstrip().startswith("#") else _LINE.fullmatch(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            if key in pending:  # a key set to None is never pending, so it is dropped
                lines.append(f"{key}={pending.pop(key)}")
            # A repeated key is dropped so the file has one definition.
            continue
        lines.append(line)
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(lines) + "\n"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _stage(path: Path, text: str) -> str:
    """Write ``text`` to a fsynced 0600 temporary file beside ``path``; return its name."""
    directory = path.parent if str(path.parent) else Path(".")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        if hasattr(os, "fchmod"):  # not on Windows before Python 3.13
            os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return temp_name


def _put(path: Path, temp_name: str) -> None:
    os.replace(temp_name, path)
    os.chmod(path, FILE_MODE)


EnvChange = tuple[Path, Mapping[str, str | None], str]


def write_envs(changes: Sequence[EnvChange]) -> None:
    """Apply several ``(path, updates, template)`` changes as one step.

    Every new file is staged first, so bad input changes nothing. If putting
    one in place fails, the files already replaced get their old text back,
    and a file that did not exist is removed again. Each file is mode 0600.
    """
    staged: list[tuple[Path, str, str | None]] = []
    try:
        for path, updates, template in changes:
            current = _read_text(path)
            text = update_env_text(template if current is None else current, updates)
            staged.append((path, _stage(path, text), current))
    except BaseException:
        for _, temp_name, _ in staged:
            Path(temp_name).unlink(missing_ok=True)
        raise
    done: list[tuple[Path, str | None]] = []
    try:
        for path, temp_name, old in staged:
            _put(path, temp_name)
            done.append((path, old))
    except BaseException:
        for path, old in reversed(done):
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _put(path, _stage(path, old))
        for _, temp_name, _ in staged:
            Path(temp_name).unlink(missing_ok=True)
        raise


def write_env(path: Path, updates: Mapping[str, str | None], *, template: str = "") -> None:
    """Set ``updates`` in the file at ``path``, creating it with mode 0600 if needed.

    A new file starts from ``template``, such as an example file's text.
    """
    write_envs([(path, updates, template)])
