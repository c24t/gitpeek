"""Command-line entry point for ``gitpeek``.

Usage:

    gitpeek                  # browse HEAD in the current directory
    gitpeek <ref>            # browse a different commit (e.g. ``HEAD~1``, a SHA)
    gitpeek -C <path>        # run as if from ``<path>`` (like ``git -C``)

Exits non-zero with a short error message when not inside a git repo or
when the requested ref doesn't resolve.
"""

from __future__ import annotations

import argparse
import sys

from gitpeek import __version__
from gitpeek.git import GitError, load_commit
from gitpeek.ui import run as run_ui


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitpeek",
        description="Read-only, hg crecord-style browser for a git commit.",
    )
    parser.add_argument(
        "ref",
        nargs="?",
        default="HEAD",
        help="Commit to browse (default: HEAD). Anything `git rev-parse` accepts.",
    )
    parser.add_argument(
        "-C",
        dest="cwd",
        metavar="PATH",
        default=None,
        help="Run as if gitpeek were started in PATH (like `git -C`).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        commit = load_commit(ref=args.ref, cwd=args.cwd)
    except GitError as exc:
        print(f"gitpeek: {exc}", file=sys.stderr)
        return 2

    if not commit.files:
        # No diff at all — show the user something useful instead of
        # dropping them into an empty curses screen.
        print(
            f"commit {commit.short_sha}  {commit.subject}\n"
            f"  no file changes (merge commit? empty commit?)",
            file=sys.stderr,
        )
        return 0

    run_ui(commit)
    return 0
