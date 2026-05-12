"""Tests for the unified-diff parser.

Curses UI is exercised by hand; the parser is the part with logic worth
pinning down with assertions, especially around the less-common cases
(adds, deletes, renames, binary).
"""

from __future__ import annotations

import textwrap

from gitpeek.diff import parse_diff


def _dedent(s: str) -> str:
    # Tests are easier to read with literal indentation; strip it before
    # feeding into the parser.
    return textwrap.dedent(s).lstrip("\n")


def test_parse_simple_modify() -> None:
    patch = _dedent(
        """
        diff --git a/foo.py b/foo.py
        index 1111111..2222222 100644
        --- a/foo.py
        +++ b/foo.py
        @@ -1,3 +1,4 @@
         keep
        -bye
        +hi
        +new
         tail
        """
    )
    files = parse_diff(patch)
    assert len(files) == 1
    f = files[0]
    assert f.path == "foo.py"
    assert f.old_path is None
    assert f.status == "M"
    assert len(f.hunks) == 1
    h = f.hunks[0]
    assert h.old_start == 1 and h.old_count == 3
    assert h.new_start == 1 and h.new_count == 4
    assert [(ln.kind, ln.text) for ln in h.lines] == [
        (" ", "keep"),
        ("-", "bye"),
        ("+", "hi"),
        ("+", "new"),
        (" ", "tail"),
    ]
    assert h.additions == 2
    assert h.deletions == 1
    assert f.additions == 2
    assert f.deletions == 1


def test_parse_added_file() -> None:
    patch = _dedent(
        """
        diff --git a/new.py b/new.py
        new file mode 100644
        index 0000000..3333333
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1,2 @@
        +hello
        +world
        """
    )
    files = parse_diff(patch)
    assert len(files) == 1
    f = files[0]
    assert f.status == "A"
    assert f.path == "new.py"
    assert f.additions == 2
    assert f.deletions == 0


def test_parse_deleted_file() -> None:
    patch = _dedent(
        """
        diff --git a/old.py b/old.py
        deleted file mode 100644
        index 4444444..0000000
        --- a/old.py
        +++ /dev/null
        @@ -1,2 +0,0 @@
        -hello
        -world
        """
    )
    files = parse_diff(patch)
    assert files[0].status == "D"
    assert files[0].additions == 0
    assert files[0].deletions == 2


def test_parse_renamed_file() -> None:
    patch = _dedent(
        """
        diff --git a/old.py b/new.py
        similarity index 95%
        rename from old.py
        rename to new.py
        index 1111111..2222222 100644
        --- a/old.py
        +++ b/new.py
        @@ -1,2 +1,2 @@
         keep
        -bye
        +hi
        """
    )
    files = parse_diff(patch)
    assert len(files) == 1
    f = files[0]
    assert f.status == "R"
    assert f.old_path == "old.py"
    assert f.path == "new.py"
    assert f.additions == 1
    assert f.deletions == 1


def test_parse_binary_file() -> None:
    patch = _dedent(
        """
        diff --git a/logo.png b/logo.png
        index 5555555..6666666 100644
        Binary files a/logo.png and b/logo.png differ
        """
    )
    files = parse_diff(patch)
    assert len(files) == 1
    assert files[0].status == "B"
    assert files[0].binary is True
    assert files[0].hunks == []


def test_parse_multiple_files_and_hunks() -> None:
    patch = _dedent(
        """
        diff --git a/a.py b/a.py
        index 1..2 100644
        --- a/a.py
        +++ b/a.py
        @@ -1,2 +1,2 @@
         x
        -y
        +z
        @@ -10 +10 @@
        -old
        +new
        diff --git a/b.py b/b.py
        index 3..4 100644
        --- a/b.py
        +++ b/b.py
        @@ -1 +1,2 @@
         keep
        +added
        """
    )
    files = parse_diff(patch)
    assert [f.path for f in files] == ["a.py", "b.py"]
    assert len(files[0].hunks) == 2
    # Second hunk used the abbreviated ``@@ -10 +10 @@`` form; counts
    # should default to 1.
    assert files[0].hunks[1].old_count == 1
    assert files[0].hunks[1].new_count == 1
    assert files[1].additions == 1
    assert files[1].deletions == 0


def test_parse_empty_diff_returns_no_files() -> None:
    assert parse_diff("") == []


def test_parse_no_newline_at_end_marker_preserved() -> None:
    patch = _dedent(
        """
        diff --git a/f b/f
        index 1..2 100644
        --- a/f
        +++ b/f
        @@ -1 +1 @@
        -old
        \\ No newline at end of file
        +new
        """
    )
    files = parse_diff(patch)
    kinds = [ln.kind for ln in files[0].hunks[0].lines]
    assert kinds == ["-", "\\", "+"]
