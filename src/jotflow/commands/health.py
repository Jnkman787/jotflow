"""`jotflow health`: is the Slack connection alive, judged by the heartbeat (#133).

It reads one file, ``JOTFLOW_HEARTBEAT_FILE`` or ``/tmp/jotflow-heartbeat``,
which ``jotflow run`` rewrites every 30 seconds while connected to Slack. It
makes no network call and reads no tokens, ``.env`` or ``jotflow.toml``, so it
works on the read-only image and as the Compose healthcheck.

Exit 0 when the heartbeat is fresh, 1 when it is stale, missing or unreadable.
"""

from __future__ import annotations

import argparse
import sys
import time

from jotflow.orchestrator.health import MAX_AGE_SECONDS, heartbeat_age, heartbeat_path

EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-age",
        type=float,
        default=MAX_AGE_SECONDS,
        metavar="SECONDS",
        help=f"How old the heartbeat may be (default: {MAX_AGE_SECONDS:g})",
    )


def run(args: argparse.Namespace) -> int:
    path = heartbeat_path()
    max_age = float(getattr(args, "max_age", MAX_AGE_SECONDS))
    age = heartbeat_age(path, time.time())
    if age is None:
        print(f"unhealthy: no heartbeat at {path}; is jotflow run up and connected?")
        return EXIT_UNHEALTHY
    if age > max_age:
        print(f"unhealthy: the last heartbeat was {age:.0f}s ago; Slack may be disconnected")
        return EXIT_UNHEALTHY
    print(f"healthy: connected to Slack, heartbeat {age:.0f}s ago")
    return EXIT_HEALTHY


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(argparse.Namespace(max_age=MAX_AGE_SECONDS)))
