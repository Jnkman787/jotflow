"""Read and update a ``.env`` file without disturbing what else is in it.

``jotflow init`` writes the Slack tokens here. Comments, blank lines, and keys
JotFlow does not own are kept as they are. The file is written atomically and
is readable only by its owner, because it holds secrets.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
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


def update_env_text(text: str, updates: Mapping[str, str]) -> str:
    """Return ``text`` with each key in ``updates`` set, replacing in place or appending."""
    for key, value in updates.items():
        _check(key, value)
    pending = dict(updates)
    lines: list[str] = []
    for line in text.splitlines():
        match = None if line.lstrip().startswith("#") else _LINE.fullmatch(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            if key in pending:
                lines.append(f"{key}={pending.pop(key)}")
            # A repeated key is dropped so the file has one definition.
            continue
        lines.append(line)
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(lines) + "\n"


def write_env(path: Path, updates: Mapping[str, str]) -> None:
    """Set ``updates`` in the file at ``path``, creating it with mode 0600 if needed."""
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    new_text = update_env_text(current, updates)
    directory = path.parent if str(path.parent) else Path(".")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    os.chmod(path, FILE_MODE)
