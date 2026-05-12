"""Command-line entry point for ``gitpeek``.

Usage:

    gitpeek                  # browse the full log of the current repo
    gitpeek <ref>            # browse the log starting at <ref> (e.g.
                             # a SHA, ``HEAD~10``, ``main..feature``)
    gitpeek -n 50            # cap the log at 50 commits
    gitpeek -C <path>        # run as if from <path> (like ``git -C``)

Exits non-zero with a short error message when not inside a git repo
or when the requested ref doesn't resolve.
"""

from __future__ import annotations

import argparse
import sys

from gitpeek import __version__
from gitpeek.git import GitError, load_log, load_uncommitted
from gitpeek.ui import run as run_ui


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitpeek",
        description="Read-only, hg crecord-style browser for a git log.",
    )
    parser.add_argument(
        "ref",
        nargs="?",
        default="HEAD",
        help=(
            "Where the log starts (default: HEAD). Anything "
            "`git rev-parse` accepts — a SHA, branch, or range."
        ),
    )
    parser.add_argument(
        "-n",
        "--max-count",
        type=int,
        default=None,
        metavar="N",
        help="Show at most N commits (default: no limit).",
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
        commits = load_log(
            ref=args.ref,
            max_count=args.max_count,
            cwd=args.cwd,
        )
    except GitError as exc:
        print(f"gitpeek: {exc}", file=sys.stderr)
        return 2

    # Prepend a synthetic "uncommitted changes" entry — same shape as
    # a commit, but flagged so the UI shows it as a working-tree
    # section rather than a real revision. We silently skip on errors
    # so a misbehaving working-tree scan can't block the log view.
    try:
        wt = load_uncommitted(cwd=args.cwd)
    except GitError:
        wt = None
    if wt is not None:
        commits.insert(0, wt)

    if not commits:
        print(f"gitpeek: no commits reachable from {args.ref}", file=sys.stderr)
        return 0

    run_ui(commits, cwd=args.cwd)
    return 0
