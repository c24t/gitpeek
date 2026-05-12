"""Tests for the UI's model-level logic.

These exercise the parts of :class:`gitpeek.ui.UI` that don't need a
real curses screen — flattening the tree, fold-state mutations, cursor
math. They're enough to catch the easy regressions; the actual
rendering is verified by hand.
"""

from __future__ import annotations

import curses

from gitpeek.diff import Commit, File, Hunk, Line
from gitpeek.ui import UI


def _toy_commit() -> Commit:
    """Build a small commit by hand: two files, two hunks total."""

    f1 = File(
        path="a.py",
        old_path=None,
        status="M",
        hunks=[
            Hunk(
                header="@@ -1,2 +1,2 @@",
                old_start=1,
                old_count=2,
                new_start=1,
                new_count=2,
                context="",
                lines=[Line(" ", "x"), Line("-", "y"), Line("+", "z")],
            ),
            Hunk(
                header="@@ -10 +10 @@",
                old_start=10,
                old_count=1,
                new_start=10,
                new_count=1,
                context="",
                lines=[Line("-", "old"), Line("+", "new")],
            ),
        ],
    )
    f2 = File(
        path="b.py",
        old_path=None,
        status="A",
        hunks=[
            Hunk(
                header="@@ -0,0 +1,1 @@",
                old_start=0,
                old_count=0,
                new_start=1,
                new_count=1,
                context="",
                lines=[Line("+", "hi")],
            ),
        ],
    )
    return Commit(
        sha="0" * 40,
        short_sha="0000000",
        author="Test <t@t>",
        date="2026-05-11",
        subject="toy",
        body="",
        files=[f1, f2],
    )


def test_initial_view_collapses_files() -> None:
    """The default UI state should show commit + files, hide hunks."""
    ui = UI(_toy_commit())
    rows = ui.visible_rows()
    kinds = [r.kind for r in rows]
    assert kinds == ["commit", "file", "file"]


def test_unfold_file_reveals_its_hunks() -> None:
    ui = UI(_toy_commit())
    # Move cursor onto the first file and unfold it.
    ui._handle_key(ord("j"))   # cursor → file a.py
    ui._handle_key(ord("l"))   # unfold a.py
    rows = ui.visible_rows()
    kinds = [r.kind for r in rows]
    assert kinds == ["commit", "file", "hunk", "hunk", "file"]


def test_l_descends_when_already_unfolded() -> None:
    """Right-arrow on an unfolded parent should step into its child."""
    ui = UI(_toy_commit())
    ui._handle_key(ord("j"))   # → file a.py
    ui._handle_key(ord("l"))   # unfold a.py
    ui._handle_key(ord("l"))   # should step to first hunk
    rows = ui.visible_rows()
    assert rows[ui.cursor].kind == "hunk"


def test_h_collapses_then_walks_up() -> None:
    ui = UI(_toy_commit())
    ui._handle_key(ord("j"))   # → file a.py
    ui._handle_key(ord("l"))   # unfold a.py
    ui._handle_key(ord("l"))   # → hunk 1
    ui._handle_key(ord("l"))   # unfold hunk 1
    ui._handle_key(ord("l"))   # → first line
    assert ui.visible_rows()[ui.cursor].kind == "line"
    ui._handle_key(ord("h"))   # line has no children, jump to parent
    assert ui.visible_rows()[ui.cursor].kind == "hunk"
    ui._handle_key(ord("h"))   # hunk is unfolded → collapse it
    assert ui.visible_rows()[ui.cursor].kind == "hunk"
    ui._handle_key(ord("h"))   # → parent file
    assert ui.visible_rows()[ui.cursor].kind == "file"


def test_J_K_jump_same_kind() -> None:
    ui = UI(_toy_commit())
    # Unfold both files so we have hunks visible.
    ui._handle_key(ord("j"))
    ui._handle_key(ord("l"))   # unfold a.py
    rows = ui.visible_rows()
    # cursor should be on file a.py
    assert rows[ui.cursor].kind == "file" and rows[ui.cursor].item.path == "a.py"
    ui._handle_key(ord("J"))   # next file
    rows = ui.visible_rows()
    assert rows[ui.cursor].item.path == "b.py"
    ui._handle_key(ord("K"))   # back to a.py
    rows = ui.visible_rows()
    assert rows[ui.cursor].item.path == "a.py"


def test_F_folds_all_ancestors() -> None:
    ui = UI(_toy_commit())
    ui._handle_key(ord("j"))   # → file
    ui._handle_key(ord("l"))   # unfold file
    ui._handle_key(ord("l"))   # → hunk
    ui._handle_key(ord("l"))   # unfold hunk
    ui._handle_key(ord("l"))   # → line
    ui._handle_key(ord("F"))   # fold everything up
    rows = ui.visible_rows()
    # After folding all ancestors, the deepest visible kind should be
    # the commit itself.
    assert all(r.kind == "commit" for r in rows)
    assert ui.cursor < len(rows)


def test_g_G_jump_top_bottom() -> None:
    ui = UI(_toy_commit())
    ui._handle_key(ord("G"))
    assert ui.cursor == len(ui.visible_rows()) - 1
    ui._handle_key(ord("g"))
    assert ui.cursor == 0


def test_q_returns_false() -> None:
    ui = UI(_toy_commit())
    assert ui._handle_key(ord("q")) is False


def test_help_consumes_next_keypress() -> None:
    ui = UI(_toy_commit())
    ui._handle_key(ord("?"))
    assert ui.help_visible is True
    # Any key dismisses help without otherwise acting.
    ui._handle_key(ord("j"))
    assert ui.help_visible is False
    assert ui.cursor == 0  # the j was eaten by the help dismissal


def test_arrow_keys_match_letters() -> None:
    """↓/↑ should behave the same as j/k."""
    ui = UI(_toy_commit())
    ui._handle_key(curses.KEY_DOWN)
    assert ui.cursor == 1
    ui._handle_key(curses.KEY_UP)
    assert ui.cursor == 0
