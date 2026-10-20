"""Which repo files a drafting run may read, by an admin's ``read_paths`` (#209).

Shared by the policy, which checks patterns at load time, by JotFlow's doc
tools, which match tree paths against them, and by the drafting runner, which
re-checks the path syntax before it asks GitHub. Nothing here does I/O.

A pattern is a relative POSIX path. Each segment is a literal, or uses ``*``
(any characters but a slash) and ``?`` (one character), or is ``**`` alone
(any number of segments, including none). A wildcard never matches a segment
that starts with a dot, so a hidden file or folder must be written out. Even
then ``.git``, ``.github/workflows`` and ``.env*`` are refused. The last
segment must end in a text extension, so only text files are ever readable.
Matching is on the exact, case-sensitive path GitHub's tree reports.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from functools import cache

MAX_PATTERNS = 20
MAX_PATTERN_LENGTH = 200
# GitHub allows longer paths, but no doc worth reading has one this long.
MAX_PATH_LENGTH = 400
TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".rst", ".adoc")
# The tree's modes for a regular file. A symlink (120000) or submodule (160000) is never read.
FILE_MODES = frozenset({"100644", "100755"})

# Per-file, per-run and listing caps (#209, decision 4).
MAX_FILE_BYTES = 64 * 1024
MAX_READS_PER_RUN = 20
MAX_BYTES_PER_RUN = 256 * 1024
MAX_LISTED = 500
TRUNCATED_MARKER = "\n\n[JotFlow: this file was cut at 64 KiB]"

# Unicode categories never allowed in a pattern or a path: controls, format
# characters such as zero-width spaces, line and paragraph separators,
# private use, and surrogates.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs"})
_REFUSED_CHARACTERS = frozenset({"\\", "%"})


class PathRefused(ValueError):
    """A pattern or path that breaks the rules. The message is fixed text."""


def _check_characters(value: str, what: str) -> None:
    for ch in value:
        if ch in _REFUSED_CHARACTERS:
            raise PathRefused(f"{what} may not contain a backslash or percent sign")
        if unicodedata.category(ch) in _REFUSED_CATEGORIES:
            raise PathRefused(f"{what} may not contain control or invisible characters")


def _forbidden(segments: Sequence[str]) -> bool:
    """Whether the segments name .git, .github/workflows or an .env file, in any case."""
    folded = [s.casefold() for s in segments]
    for i, segment in enumerate(folded):
        if segment == ".git" or segment.startswith(".env"):
            return True
        if segment == ".github" and i + 1 < len(folded) and folded[i + 1] == "workflows":
            return True
    return False


def _has_text_extension(name: str) -> bool:
    return name.casefold().endswith(TEXT_EXTENSIONS)


def check_path(path: str) -> str:
    """``path`` if it's a plain relative file path a doc could have, else PathRefused.

    The syntax check only: whether a pattern allows it is :func:`allowed`.
    """
    if not path or len(path) > MAX_PATH_LENGTH:
        raise PathRefused("a path must be 1 to 400 characters")
    _check_characters(path, "a path")
    if path.startswith("/"):
        raise PathRefused("a path must be relative")
    segments = path.split("/")
    if any(s in ("", ".", "..") for s in segments):
        raise PathRefused("a path may not have empty, . or .. segments")
    if _forbidden(segments):
        raise PathRefused("a path may not be in .git or .github/workflows, or an .env file")
    if not _has_text_extension(segments[-1]):
        raise PathRefused("only text files may be read: " + ", ".join(TEXT_EXTENSIONS))
    return path


def check_pattern(pattern: str) -> str:
    """``pattern`` if it follows the rules in this module's docstring, else PathRefused."""
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        raise PathRefused(f"a read_paths pattern must be 1 to {MAX_PATTERN_LENGTH} characters")
    _check_characters(pattern, "a read_paths pattern")
    if pattern.startswith("/"):
        raise PathRefused("a read_paths pattern must be relative, without a leading /")
    segments = pattern.split("/")
    if any(s in ("", ".", "..") for s in segments):
        raise PathRefused("a read_paths pattern may not have empty, . or .. segments")
    for segment in segments:
        if "**" in segment and segment != "**":
            raise PathRefused("** must be a whole segment, as in docs/**/*.md")
    if _forbidden(segments):
        raise PathRefused(
            "read_paths may not reach .git, .github/workflows or .env files, even written out"
        )
    if segments[-1] == "**" or not _has_text_extension(segments[-1]):
        raise PathRefused(
            "a read_paths pattern must end in a text file name: " + ", ".join(TEXT_EXTENSIONS)
        )
    return pattern


def check_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    if not patterns:
        raise PathRefused("an empty read_paths list is ambiguous; remove the line instead")
    if len(patterns) > MAX_PATTERNS:
        raise PathRefused(f"read_paths takes at most {MAX_PATTERNS} patterns")
    if len(set(patterns)) != len(patterns):
        raise PathRefused("read_paths lists a pattern more than once")
    return tuple(check_pattern(p) for p in patterns)


def _segment_matches(pattern: str, name: str) -> bool:
    """``*`` and ``?`` within one segment. A wildcard never matches a leading dot."""
    if name.startswith(".") and not pattern.startswith("."):
        return False
    # Iterative wildcard matching with one backtrack point: linear in practice,
    # never exponential.
    p = n = 0
    star = -1
    mark = 0
    while n < len(name):
        if p < len(pattern) and (pattern[p] == "?" or pattern[p] == name[n]):
            p += 1
            n += 1
        elif p < len(pattern) and pattern[p] == "*":
            star, mark = p, n
            p += 1
        elif star >= 0:
            p = star + 1
            mark += 1
            n = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def matches(pattern: str, path: str) -> bool:
    """Whether a checked ``pattern`` allows a ``path`` from GitHub's tree."""
    want = tuple(pattern.split("/"))
    have = tuple(path.split("/"))

    @cache
    def at(i: int, j: int) -> bool:
        if i == len(want):
            return j == len(have)
        if want[i] == "**":
            # None or more segments, none of them hidden.
            if at(i + 1, j):
                return True
            return j < len(have) and not have[j].startswith(".") and at(i, j + 1)
        return j < len(have) and _segment_matches(want[i], have[j]) and at(i + 1, j + 1)

    return at(0, 0)


def allowed(patterns: Sequence[str], path: str) -> bool:
    """Whether ``path`` is a valid doc path that one of ``patterns`` allows."""
    try:
        check_path(path)
    except PathRefused:
        return False
    return any(matches(p, path) for p in patterns)
