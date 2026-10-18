"""`jotflow replay`: run a saved thread through the real pipeline, offline (#17, D4).

Takes a built-in fixture's name, such as ``feature-request``, or a path to a
``jotflow-thread/1`` file. Prints a report naming messages by ``ts`` only, and
exits 0 when the outcome matches the file's ``expected``, 1 when it does not,
and 2 for a usage or file problem. ``--db`` keeps the replayed store in a
SQLite file instead of memory.

``--agent <preset>`` runs the fixture's drafting runs with a real agent instead
of its script (#23; see :mod:`jotflow.replay_agent`). The agent's model login
comes from your environment, and each run costs model tokens. ``--agent
scripted`` plays the script over real MCP, with no model. ``--all`` replays
every built-in fixture, and ``--repeat N`` runs each N times and prints how
often each matched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from jotflow.models import RunKind
from jotflow.replay import replay
from jotflow.replay_agent import AGENTS, DEFAULT_TIME_LIMIT, AgentOptions, repeat
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
    parser.add_argument(
        "--agent",
        choices=AGENTS,
        default=None,
        help="run a real agent preset instead of the script; it uses your model login",
    )
    parser.add_argument("--all", action="store_true", help="replay every built-in fixture")
    parser.add_argument(
        "--repeat", type=int, default=1, help="with --agent: run each fixture this many times"
    )
    parser.add_argument("--model", default=None, help="with --agent: the model for drafting runs")
    parser.add_argument(
        "--time-limit",
        type=float,
        default=DEFAULT_TIME_LIMIT,
        help=f"with --agent: seconds per run (default: {DEFAULT_TIME_LIMIT:g})",
    )


def _names() -> list[str]:
    return sorted(path.stem for path in builtin_paths())


def run(args: argparse.Namespace) -> int:
    if getattr(args, "list", False):
        print("\n".join(_names()))
        return EXIT_MATCH
    agent = getattr(args, "agent", None)
    times = getattr(args, "repeat", 1)
    if times < 1 or (times > 1 and agent is None):
        print("jotflow replay: --repeat needs --agent and a count of 1 or more", file=sys.stderr)
        return EXIT_USAGE
    if agent is not None and getattr(args, "db", None) is not None:
        print("jotflow replay: --db and --agent cannot be combined", file=sys.stderr)
        return EXIT_USAGE
    if getattr(args, "all", False):
        if getattr(args, "thread", None):
            print("jotflow replay: give a thread or --all, not both", file=sys.stderr)
            return EXIT_USAGE
        threads = [(n, builtin_thread(n)) for n in _names()]
        if agent is None:
            return asyncio.run(_replay_all(threads))
        return asyncio.run(_with_agent(threads, args))
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
    if agent is not None:
        return asyncio.run(_with_agent([(name, thread)], args))
    return asyncio.run(_replay(thread, getattr(args, "db", None)))


async def _replay_all(threads: list[tuple[str, SavedThread]]) -> int:
    ok = True
    for _, thread in threads:
        report = await replay(thread)
        print(report.render(), end="\n\n")
        ok = ok and report.ok
    return EXIT_MATCH if ok else EXIT_MISMATCH


async def _with_agent(threads: list[tuple[str, SavedThread]], args: argparse.Namespace) -> int:
    model = getattr(args, "model", None)
    ok = True
    for ref, thread in threads:
        options = AgentOptions(
            agent=args.agent,
            thread_ref=ref,
            time_limit=getattr(args, "time_limit", DEFAULT_TIME_LIMIT),
            models={RunKind.DRAFTING: model} if model else {},
        )
        tally, reports = await repeat(thread, options, getattr(args, "repeat", 1))
        if len(reports) == 1:
            print(reports[0].render(), end="\n\n")
        else:
            for report in reports:
                if not report.ok:
                    print(report.render(), end="\n\n")
            print(tally.render(), end="\n\n")
        ok = ok and tally.passed == tally.runs
    return EXIT_MATCH if ok else EXIT_MISMATCH


async def _replay(thread: SavedThread, db: Path | None) -> int:
    store: Store | None = await SqliteStore.open(db) if db else None
    try:
        report = await replay(thread, store=store)
    finally:
        if store is not None:
            await store.close()
    print(report.render())
    return EXIT_MATCH if report.ok else EXIT_MISMATCH
