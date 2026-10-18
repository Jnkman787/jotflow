"""`jotflow run`. Placeholder until its issue lands."""

from __future__ import annotations

import argparse
import sys


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register this command's options."""


def run(args: argparse.Namespace) -> int:
    print("jotflow run is not implemented yet.", file=sys.stderr)
    return 2
