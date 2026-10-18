"""`jotflow replay`: run a saved thread through the real pipeline, offline (#17, D4).

Takes a built-in fixture's name, such as ``feature-request``, or a path to a
``jotflow-thread/1`` file. Prints a report naming messages by ``ts`` only, and
exits 0 when the outcome matches the file's ``expected``, 1 when it does not,
and 2 for a usage or file problem. ``--db`` keeps the replayed store in a
SQLite file instead of memory.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from jotflow.replay import replay
from jotflow.store import SqliteStore, Store
from jotflow.testing.threads import (
    SavedThread,
    ThreadError,
    builtin_paths,
    builtin_thread,
    load_thread,
)

EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register this command's options."""
    parser.add_argument(
        "thread",
        nargs="?",
        help="a built-in fixture name, such as feature-request, or a path to a saved thread",
    )
    parser.add_argument("--list", action="store_true", help="list the built-in fixtures")
    parser.add_argument(
        "--db", type=Path, default=None, help="keep the replayed store in this SQLite file"
    )


def _names() -> list[str]:
    return sorted(path.stem for path in builtin_paths())


def run(args: argparse.Namespace) -> int:
    if getattr(args, "list", False):
        print("\n".join(_names()))
        return EXIT_MATCH
    name = getattr(args, "thread", None)
    if not name:
        print(
            "jotflow replay needs a saved thread: a built-in name such as feature-request, "
            "or a file path. See them with: jotflow replay --list",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        thread = load_thread(name) if Path(name).suffix == ".json" else builtin_thread(name)
    except ThreadError as exc:
        print(f"jotflow replay: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(_replay(thread, getattr(args, "db", None)))


async def _replay(thread: SavedThread, db: Path | None) -> int:
    store: Store | None = await SqliteStore.open(db) if db else None
    try:
        report = await replay(thread, store=store)
    finally:
        if store is not None:
            await store.close()
    print(report.render())
    return EXIT_MATCH if report.ok else EXIT_MISMATCH
