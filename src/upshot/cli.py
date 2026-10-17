"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from upshot import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upshot", description="Turn team chat into GitHub issues."
    )
    parser.add_argument("--version", action="version", version=f"upshot {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
