"""Thin subprocess wrappers around the ``git`` CLI.

We talk to git through its porcelain rather than libgit2 / pygit2 for
two reasons: zero install footprint, and the output formats we need
(``log -1 --format=...`` and ``show --patch``) are stable and easy to
parse. The only subtle bit is using NUL-byte separators in the log
format so commit subjects and bodies can contain anything.
"""

from __future__ import annotations

import subprocess

from gitpeek.diff import Commit, Message, parse_diff


# Field separator inside ``git log --format``. NUL is safe because git
# guarantees it cannot appear in any of the placeholders we use. We
# spell it as the literal ``%x00`` git-format escape so the argument
# itself contains no real NUL byte (subprocess rejects those in argv);
# git expands the escape to a NUL in its output, which is what we split
# on below.
_SEP_FMT = "%x00"
_SEP = "\x00"


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


def load_commit(ref: str = "HEAD", cwd: str | None = None) -> Commit:
    """Load metadata + diff for ``ref`` and return a populated Commit.

    Two git invocations: one for the metadata (so we can split on NULs)
    and one for the diff (which we hand straight to :func:`parse_diff`).
    Splitting the calls is simpler than trying to interleave the two
    formats in a single ``git show`` invocation.
    """

    ensure_repo(cwd)

    fmt = _SEP_FMT.join(["%H", "%h", "%an <%ae>", "%ad", "%s", "%b"])
    meta = _run(
        ["log", "-1", f"--format={fmt}", "--date=iso", ref],
        cwd=cwd,
    ).rstrip("\n")
    # ``maxsplit=5`` so a body containing NULs (extremely unlikely, but)
    # stays intact in the final field.
    parts = meta.split(_SEP, 5)
    if len(parts) != 6:
        raise GitError(f"unexpected git log output: {meta!r}")
    sha, short_sha, author, date, subject, body = parts

    patch = _run(
        ["show", ref, "--format=", "--no-color", "--patch", "-U3"],
        cwd=cwd,
    )
    files = parse_diff(patch)

    return Commit(
        sha=sha,
        short_sha=short_sha,
        author=author,
        date=date,
        subject=subject,
        # ``%b`` emits a trailing newline for non-empty bodies, which
        # would render as a stray blank line at the end of the prose.
        # Strip it once here so every consumer sees the same shape.
        message=Message(body=body.rstrip("\n")),
        files=files,
    )
