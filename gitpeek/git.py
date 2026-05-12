"""Thin subprocess wrappers around the ``git`` CLI.

We talk to git through its porcelain rather than libgit2 / pygit2 for
two reasons: zero install footprint, and the output formats we need
(``log --format=...`` and ``show --patch``) are stable and easy to
parse. The only subtle bit is the separator dance: ``git log -z``
inserts a NUL between commits, and inside each commit we use SOH
(``%x01``) as the field separator. Both characters are extraordinarily
unlikely to appear inside an author name or commit message, and using
two distinct sentinels means we can parse the stream without ambiguity.
"""

from __future__ import annotations

import subprocess
from typing import List

from gitpeek.diff import Commit, File, Message, parse_diff


# Field separator *inside* a single commit's log record. We spell it as
# the literal git-format escape ``%x01`` so the argv we hand to
# subprocess contains no real control byte (subprocess rejects NULs;
# SOH is also safer to keep out of argv). Git expands the escape to a
# real SOH in its output, which is what we split on below.
_FIELD_FMT = "%x01"
_FIELD = "\x01"
# Record separator *between* commits in ``git log -z`` output: a single
# NUL byte. ``-z`` is git's documented "machine-parseable log" mode.
_RECORD = "\x00"


class GitError(RuntimeError):
    """Raised when a git invocation fails or we're not inside a repo."""


def _run(args: list[str], cwd: str | None = None) -> str:
    """Run ``git <args>`` and return stdout as text.

    Stderr is captured and surfaced through :class:`GitError` so the UI
    can show the user a meaningful message instead of a traceback.
    """

    try:
        result = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.strip() or f"git {' '.join(args)} failed"
        raise GitError(msg) from exc
    return result.stdout


def ensure_repo(cwd: str | None = None) -> None:
    """Raise :class:`GitError` if ``cwd`` isn't inside a git repository."""

    _run(["rev-parse", "--is-inside-work-tree"], cwd=cwd)


def load_log(
    ref: str = "HEAD",
    max_count: int | None = None,
    cwd: str | None = None,
) -> List[Commit]:
    """Load metadata for every commit reachable from ``ref``.

    Returns a list of :class:`Commit` objects with empty ``files`` —
    the diff is fetched lazily via :func:`load_diff` when the UI
    actually opens a commit. The metadata pass is one ``git log``
    invocation regardless of repo size, so even a 100k-commit history
    paginates in well under a second.

    ``max_count`` caps the log (passed through as ``-n``); ``None``
    means no limit. ``ref`` can be any rev-parseable spec — a SHA, a
    branch name, ``HEAD~5..HEAD``, etc.
    """

    ensure_repo(cwd)

    # Author name only (``%an``) — we deliberately drop the email here
    # because the log view is space-constrained and the name alone is
    # what humans scan for. The email is still recoverable from
    # ``git show`` if someone really wants it.
    fmt = _FIELD_FMT.join(["%H", "%h", "%an", "%ad", "%s", "%b"])
    args = ["log", "-z", f"--format={fmt}", "--date=iso"]
    if max_count is not None:
        args.extend(["-n", str(max_count)])
    args.append(ref)

    raw = _run(args, cwd=cwd)
    # ``-z`` terminates the *last* record with a NUL too, so a trailing
    # empty string would show up after the split. Drop it before
    # iterating to avoid an "unexpected empty record" branch below.
    if raw.endswith(_RECORD):
        raw = raw[:-1]
    if not raw:
        return []

    commits: list[Commit] = []
    for record in raw.split(_RECORD):
        # Git can prepend a stray newline to records after the first
        # under certain ``--format`` shapes; strip it so the SHA field
        # parses cleanly.
        record = record.lstrip("\n")
        parts = record.split(_FIELD, 5)
        if len(parts) != 6:
            # Malformed record — skip rather than crash the whole load.
            # In practice this only fires when someone has shoved a
            # literal SOH into a commit message, which we'd rather
            # quietly drop than refuse to show the rest of the log.
            continue
        sha, short_sha, author, date, subject, body = parts
        commits.append(
            Commit(
                sha=sha,
                short_sha=short_sha,
                author=author,
                date=date,
                subject=subject,
                # ``%b`` adds a trailing newline for non-empty bodies;
                # strip it so the message subtree doesn't end on a
                # phantom blank line.
                message=Message(body=body.rstrip("\n")),
            )
        )
    return commits


def load_diff(sha: str, cwd: str | None = None) -> List[File]:
    """Fetch and parse the diff for a single commit.

    Called by the UI on first unfold of a commit row. Three unified
    context lines (``-U3``) matches what git's own porcelain emits and
    keeps hunk bodies readable without bloating them.
    """

    patch = _run(
        ["show", sha, "--format=", "--no-color", "--patch", "-U3"],
        cwd=cwd,
    )
    return parse_diff(patch)


def load_commit(ref: str = "HEAD", cwd: str | None = None) -> Commit:
    """Convenience: load a single commit fully populated with its diff.

    Thin wrapper over :func:`load_log` (with ``max_count=1``) plus
    :func:`load_diff`. Kept around because it's the simplest API for
    callers who want one commit and don't care about the log view —
    most notably the unit tests and ad-hoc scripting.
    """

    commits = load_log(ref=ref, max_count=1, cwd=cwd)
    if not commits:
        raise GitError(f"no commits matching {ref!r}")
    c = commits[0]
    c.files = load_diff(c.sha, cwd=cwd)
    c._loaded = True
    return c
