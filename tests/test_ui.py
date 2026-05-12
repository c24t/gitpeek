"""Tests for the UI's model-level logic.

These exercise the parts of :class:`gitpeek.ui.UI` that don't need a
real curses screen — flattening the tree, fold-state mutations, cursor
math. They're enough to catch the easy regressions; the actual
rendering is verified by hand.
"""

from __future__ import annotations

import curses

from gitpeek.diff import Commit, File, Hunk, Line, Message
from gitpeek.ui import UI


def _toy_commit(body: str = "") -> Commit:
    """Build a small commit by hand: two files, two hunks total.

    ``body`` lets a test attach a non-empty message body without
    re-stating the whole file/hunk skeleton; it defaults to empty so
    the bulk of the tree-navigation tests see no message row at all.
    """

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
        message=Message(body=body),
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


def test_zR_unfolds_every_file_and_hunk() -> None:
    """``zR`` should open every file and hunk in the tree at once."""
    ui = UI(_toy_commit())
    ui._handle_key(ord("z"))
    assert ui._pending_z is True
    ui._handle_key(ord("R"))
    assert ui._pending_z is False
    # Every file + every hunk should now be open.
    for f in ui.commit.files:
        assert f.folded is False
        for h in f.hunks:
            assert h.folded is False
    kinds = [r.kind for r in ui.visible_rows()]
    # Lines still folded by default; we only flipped files & hunks.
    assert "hunk" in kinds and kinds.count("file") == 2


def test_zM_folds_every_file_and_hunk_but_keeps_commit_open() -> None:
    ui = UI(_toy_commit())
    # First unfold everything so zM has something to do.
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    # Now collapse it all.
    ui._handle_key(ord("z"))
    ui._handle_key(ord("M"))
    for f in ui.commit.files:
        assert f.folded is True
        for h in f.hunks:
            assert h.folded is True
    # Commit row deliberately stays open — orientation.
    assert ui.commit.folded is False
    kinds = [r.kind for r in ui.visible_rows()]
    assert kinds == ["commit", "file", "file"]


def test_z_followed_by_unknown_key_is_silent_noop() -> None:
    """A stray ``j`` after ``z`` should not double as cancel + move."""
    ui = UI(_toy_commit())
    ui._handle_key(ord("z"))
    ui._handle_key(ord("j"))
    assert ui._pending_z is False
    # Cursor must not have advanced — the ``j`` was swallowed by the
    # pending prefix the same way vim eats the second key.
    assert ui.cursor == 0


def test_star_opens_subtree_when_anything_closed() -> None:
    """On a file row, ``*`` should unfold the file and every hunk under it."""
    ui = UI(_toy_commit())
    ui._handle_key(ord("j"))   # cursor → file a.py (folded by default)
    ui._handle_key(ord("*"))
    a = ui.commit.files[0]
    assert a.folded is False
    for h in a.hunks:
        assert h.folded is False
    # The second file should still be untouched.
    assert ui.commit.files[1].folded is True


def test_star_closes_subtree_only_when_fully_open() -> None:
    """``*`` flips to closed only when *everything* in the subtree is open."""
    ui = UI(_toy_commit())
    ui._handle_key(ord("j"))   # → file a.py
    ui._handle_key(ord("*"))   # fully open
    a = ui.commit.files[0]
    assert a.folded is False and all(not h.folded for h in a.hunks)
    ui._handle_key(ord("*"))   # everything was open → fold subtree
    assert a.folded is True
    for h in a.hunks:
        assert h.folded is True


def test_star_with_partial_state_opens_rather_than_closes() -> None:
    """If even one descendant is closed, ``*`` opens — never closes."""
    ui = UI(_toy_commit())
    ui._handle_key(ord("j"))   # → file a.py
    ui._handle_key(ord("*"))   # fully open
    # Manually re-fold one hunk to create a mixed state.
    ui.commit.files[0].hunks[0].folded = True
    ui._invalidate()
    ui._handle_key(ord("*"))
    a = ui.commit.files[0]
    assert a.folded is False
    assert all(not h.folded for h in a.hunks)


def test_star_on_commit_row_toggles_whole_tree() -> None:
    ui = UI(_toy_commit())
    # Cursor starts on the commit row.
    ui._handle_key(ord("*"))   # anything closed → open all
    assert ui.commit.folded is False
    for f in ui.commit.files:
        assert f.folded is False
        for h in f.hunks:
            assert h.folded is False
    ui._handle_key(ord("*"))   # everything open → close all in subtree
    # Commit itself is in the subtree, so it folds; that's fine here
    # because the user explicitly asked for "everything below me".
    assert ui.commit.folded is True


def test_no_message_row_when_body_is_empty() -> None:
    """Empty commit messages should not produce a placeholder row."""
    ui = UI(_toy_commit())
    kinds = [r.kind for r in ui.visible_rows()]
    assert "message" not in kinds
    # Just commit + two files at the top level.
    assert kinds == ["commit", "file", "file"]


def test_message_row_appears_above_files_when_body_present() -> None:
    ui = UI(_toy_commit(body="First line of body\n\nSecond paragraph"))
    kinds = [r.kind for r in ui.visible_rows()]
    # Message must sit *between* the commit row and the first file row,
    # matching the natural ``git log -p`` ordering of why-then-what.
    assert kinds.index("message") == 1
    assert kinds.index("message") < kinds.index("file")


def test_message_unfolds_to_show_body_lines() -> None:
    body = "Line one\n\nLine three"
    ui = UI(_toy_commit(body=body))
    ui._handle_key(ord("j"))   # cursor → message row
    ui._handle_key(ord("l"))   # unfold it
    rows = ui.visible_rows()
    msg_lines = [r.item for r in rows if r.kind == "message_line"]
    # splitlines preserves the blank line between paragraphs — the user
    # should see the prose exactly as ``git log`` would render it.
    assert msg_lines == ["Line one", "", "Line three"]


def test_star_on_commit_expands_message_too() -> None:
    """Subtree toggle on a commit row should reach into the message."""
    ui = UI(_toy_commit(body="body line"))
    ui._handle_key(ord("*"))
    assert ui.commit.message.folded is False
    assert "message_line" in [r.kind for r in ui.visible_rows()]


def test_zR_unfolds_message_along_with_everything_else() -> None:
    ui = UI(_toy_commit(body="hello"))
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    assert ui.commit.message.folded is False


def test_star_on_leaf_line_is_noop() -> None:
    ui = UI(_toy_commit())
    # Drill all the way down to a line row.
    ui._handle_key(ord("j"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("l"))
    assert ui.visible_rows()[ui.cursor].kind == "line"
    before = [(f.folded, [h.folded for h in f.hunks]) for f in ui.commit.files]
    ui._handle_key(ord("*"))
    after = [(f.folded, [h.folded for h in f.hunks]) for f in ui.commit.files]
    assert before == after
