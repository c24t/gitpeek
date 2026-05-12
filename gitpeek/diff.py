"""Data model + unified-diff parser for gitpeek.

The model is a plain hierarchy:

    Commit
      └── File
            └── Hunk
                  └── Line

Each node carries a ``folded`` flag that the UI mutates to hide/reveal
children. Parsing is intentionally minimal: we recognise the common
``git diff`` shapes (modify, add, delete, rename, binary) and degrade
gracefully on anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Recognise the start of a per-file diff and the start of a hunk. Both
# patterns are anchored at line start; the rest of the parser walks the
# diff line-by-line so it can carry state between header and body.
_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


@dataclass
class Line:
    """One line within a hunk body.

    ``kind`` is the single-character marker from the unified-diff format:
    ``' '`` for context, ``'+'`` for additions, ``'-'`` for removals, and
    ``'\\'`` for the "No newline at end of file" sentinel. ``text`` is
    the line content with the marker stripped.
    """

    kind: str
    text: str


@dataclass
class Hunk:
    """One ``@@ -a,b +c,d @@`` region within a file's diff."""

    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str
    lines: list[Line] = field(default_factory=list)
    folded: bool = False

    @property
    def additions(self) -> int:
        return sum(1 for ln in self.lines if ln.kind == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for ln in self.lines if ln.kind == "-")


@dataclass
class File:
    """One file in the diff.

    ``status`` mirrors git's short status letters: ``'M'`` modified,
    ``'A'`` added, ``'D'`` deleted, ``'R'`` renamed, ``'B'`` binary. We
    keep ``old_path`` for renames so the UI can show ``a → b``.
    """

    path: str
    old_path: str | None
    status: str
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    folded: bool = False

    @property
    def additions(self) -> int:
        return sum(h.additions for h in self.hunks)

    @property
    def deletions(self) -> int:
        return sum(h.deletions for h in self.hunks)


@dataclass
class Message:
    """The commit message body, presented as a foldable subtree.

    The summary (subject) is shown on the commit row itself; this node
    carries everything *after* the blank line — the prose body — so it
    can be hidden by default and unfolded on demand. An empty body is
    treated as "no message node at all": the UI omits the row entirely
    rather than showing an empty placeholder.
    """

    body: str
    folded: bool = True

    @property
    def lines(self) -> list[str]:
        """Body split into display lines.

        ``"".splitlines()`` returns ``[]``, which is exactly what we want
        — an empty body means no children and the UI suppresses the
        message row. Internal blank lines (``""``) are preserved so the
        rendered prose reads the same as ``git log``.
        """

        return self.body.splitlines()


@dataclass
class Commit:
    """A single commit in the log being browsed.

    ``files`` is empty and ``_loaded`` is ``False`` until the UI
    triggers a diff fetch — see :func:`gitpeek.git.load_diff`. This
    lazy split lets the log view show thousands of commits cheaply and
    only pay the per-commit ``git show`` cost when the user actually
    opens one.
    """

    sha: str
    short_sha: str
    author: str
    date: str
    subject: str
    message: Message
    files: list[File] = field(default_factory=list)
    # In the log view every commit starts folded — the user is looking
    # at a list of subjects, not at a wall of diffs — but we keep the
    # flag here so the UI's fold logic can treat all node types
    # uniformly.
    folded: bool = True
    # ``True`` once we've fetched this commit's diff. Used by the UI to
    # gate lazy loading (don't refetch what's already here) and to
    # decide whether to show the ``(N files)`` suffix on the commit
    # row (showing ``(0 files)`` before loading would be a lie).
    _loaded: bool = False


def parse_diff(text: str) -> list[File]:
    """Parse a ``git show`` / ``git diff`` patch into a list of files.

    The parser is line-driven rather than regex-driven for the body
    because we need to associate context/added/removed lines with the
    currently-open hunk, and we need to recognise where one file ends
    and the next begins.
    """

    files: list[File] = []
    current_file: File | None = None
    current_hunk: Hunk | None = None

    # Splitlines drops the trailing newline on each line, which is what
    # we want — the unified-diff format doesn't carry meaningful trailing
    # whitespace and we'd just have to strip it.
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        m = _DIFF_HEADER.match(line)
        if m:
            # A new file. Close out the previous one (if any) and start
            # fresh. We optimistically classify as "modified" and refine
            # after scanning the header block.
            if current_file is not None:
                files.append(current_file)
            old_path, new_path = m.group(1), m.group(2)
            current_file = File(
                path=new_path,
                old_path=old_path if old_path != new_path else None,
                status="M",
            )
            current_hunk = None
            i += 1
            # Header block: everything between ``diff --git`` and the
            # first ``@@`` (or the next ``diff --git``). The lines we
            # care about are the status hints and the binary marker.
            while i < len(lines):
                hline = lines[i]
                if hline.startswith("diff --git ") or hline.startswith("@@"):
                    break
                if hline.startswith("new file"):
                    current_file.status = "A"
                elif hline.startswith("deleted file"):
                    current_file.status = "D"
                elif hline.startswith("rename from "):
                    current_file.status = "R"
                    current_file.old_path = hline[len("rename from "):]
                elif hline.startswith("rename to "):
                    current_file.path = hline[len("rename to "):]
                elif hline.startswith("Binary files"):
                    current_file.binary = True
                    current_file.status = "B"
                i += 1
            continue

        m = _HUNK_HEADER.match(line)
        if m and current_file is not None:
            # Counts default to 1 when omitted (``@@ -5 +5 @@`` means
            # ``-5,1 +5,1``). This matches git's own behaviour.
            old_count = int(m.group(2)) if m.group(2) else 1
            new_count = int(m.group(4)) if m.group(4) else 1
            current_hunk = Hunk(
                header=line,
                old_start=int(m.group(1)),
                old_count=old_count,
                new_start=int(m.group(3)),
                new_count=new_count,
                context=m.group(5),
            )
            current_file.hunks.append(current_hunk)
            i += 1
            continue

        if current_hunk is not None and line and line[0] in " +-\\":
            # Body line. ``\ No newline at end of file`` is rare but
            # legal; we preserve it as a context-like marker so the user
            # can see git is telling them something.
            current_hunk.lines.append(Line(kind=line[0], text=line[1:]))
            i += 1
            continue

        # Anything else — blank lines between sections, ``index ...``
        # headers we didn't catch, etc. — is skipped silently.
        i += 1

    if current_file is not None:
        files.append(current_file)
    return files
