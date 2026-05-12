"""Tests for the UI's model-level logic.

These exercise the parts of :class:`gitpeek.ui.UI` that don't need a
real curses screen — flattening the tree, fold-state mutations, cursor
math, lazy-loading hooks. They're enough to catch the easy regressions;
the actual rendering is verified by hand.

The toy commits are hand-built and pre-loaded so the tests don't have
to mock out git. Lazy-loading is tested explicitly via a stub on the
``load_diff`` symbol that the UI imports.
"""

from __future__ import annotations

import curses

import gitpeek.ui as ui_module
from gitpeek.diff import Commit, File, Hunk, Line, Message
from gitpeek.ui import UI


def _toy_commit(body: str = "", subject: str = "toy", sha: str = "0") -> Commit:
    """Build a small commit by hand: two files, two hunks total.

    The commit is returned ``folded=False`` and ``_loaded=True`` so
    tests start with the same "commit row open, files folded" view the
    old single-commit UI provided. ``body`` lets a test attach a
    non-empty message body without re-stating the file skeleton.
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
        sha=sha * 40,
        short_sha=sha * 7,
        author="Test",
        date="2026-05-11 12:00:00 -0700",
        subject=subject,
        message=Message(body=body),
        files=[f1, f2],
        folded=False,
        _loaded=True,
    )


def _toy_working_tree(*, files: list[File] | None = None) -> Commit:
    """Return a synthetic ``working tree`` commit for UI tests.

    Mirrors what :func:`gitpeek.git.load_uncommitted` produces: empty
    sha / author / date, ``is_working_tree=True``, eagerly loaded.
    """

    return Commit(
        sha="",
        short_sha="working tree",
        author="",
        date="",
        subject="Uncommitted changes",
        message=Message(body=""),
        files=files if files is not None else [],
        folded=True,
        _loaded=True,
        is_working_tree=True,
    )


def _stub_metadata_commit(sha: str, subject: str = "stub") -> Commit:
    """Return a commit with only metadata — no files, ``_loaded=False``.

    Used to drive the lazy-load codepath. Mirrors what
    :func:`gitpeek.git.load_log` produces for the unopened entries in a
    log view.
    """

    return Commit(
        sha=sha * 40,
        short_sha=sha * 7,
        author="Stub",
        date="2026-05-11 12:00:00 -0700",
        subject=subject,
        message=Message(body=""),
        files=[],
        folded=True,
        _loaded=False,
    )


# -- existing single-commit navigation ---------------------------------


def test_initial_view_collapses_files() -> None:
    """The default UI state should show commit + files, hide hunks."""
    ui = UI([_toy_commit()])
    rows = ui.visible_rows()
    kinds = [r.kind for r in rows]
    assert kinds == ["commit", "file", "file"]


def test_unfold_file_reveals_its_hunks() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))   # cursor → file a.py
    ui._handle_key(ord("l"))   # unfold a.py
    kinds = [r.kind for r in ui.visible_rows()]
    assert kinds == ["commit", "file", "hunk", "hunk", "file"]


def test_l_descends_when_already_unfolded() -> None:
    """Right-arrow on an unfolded parent should step into its child."""
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))   # → file a.py
    ui._handle_key(ord("l"))   # unfold a.py
    ui._handle_key(ord("l"))   # step to first hunk
    assert ui.visible_rows()[ui.cursor].kind == "hunk"


def test_h_collapses_then_walks_up() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))   # → file
    ui._handle_key(ord("l"))   # unfold file
    ui._handle_key(ord("l"))   # → hunk
    ui._handle_key(ord("l"))   # unfold hunk
    ui._handle_key(ord("l"))   # → line
    assert ui.visible_rows()[ui.cursor].kind == "line"
    ui._handle_key(ord("h"))   # line has no children → jump to parent
    assert ui.visible_rows()[ui.cursor].kind == "hunk"
    ui._handle_key(ord("h"))   # hunk unfolded → collapse it
    assert ui.visible_rows()[ui.cursor].kind == "hunk"
    ui._handle_key(ord("h"))   # → parent file
    assert ui.visible_rows()[ui.cursor].kind == "file"


def test_J_K_jump_same_kind() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))
    ui._handle_key(ord("l"))   # unfold a.py so we can verify the jump skips its hunks
    rows = ui.visible_rows()
    assert rows[ui.cursor].kind == "file" and rows[ui.cursor].item.path == "a.py"
    ui._handle_key(ord("J"))   # next file
    rows = ui.visible_rows()
    assert rows[ui.cursor].item.path == "b.py"
    ui._handle_key(ord("K"))   # back to a.py
    rows = ui.visible_rows()
    assert rows[ui.cursor].item.path == "a.py"


def test_F_folds_all_ancestors() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("l"))
    ui._handle_key(ord("F"))
    rows = ui.visible_rows()
    # After folding the chain up, the deepest visible kind should be
    # the commit itself.
    assert all(r.kind == "commit" for r in rows)
    assert ui.cursor < len(rows)


def test_g_G_jump_top_bottom() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("G"))
    assert ui.cursor == len(ui.visible_rows()) - 1
    ui._handle_key(ord("g"))
    assert ui.cursor == 0


def test_q_returns_false() -> None:
    ui = UI([_toy_commit()])
    assert ui._handle_key(ord("q")) is False


def test_help_consumes_next_keypress() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("?"))
    assert ui.help_visible is True
    ui._handle_key(ord("j"))
    assert ui.help_visible is False
    assert ui.cursor == 0   # ``j`` was eaten by the help dismissal


def test_arrow_keys_match_letters() -> None:
    """↓/↑ should behave the same as j/k."""
    ui = UI([_toy_commit()])
    ui._handle_key(curses.KEY_DOWN)
    assert ui.cursor == 1
    ui._handle_key(curses.KEY_UP)
    assert ui.cursor == 0


# -- zR / zM / * --------------------------------------------------------


def test_space_toggles_fold_like_f() -> None:
    """``SPACE`` and ``f`` should behave identically: flip the fold on

    the current node, lazy-loading commits when needed."""
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))   # cursor → first file (folded by default)
    ui._handle_key(ord(" "))   # unfold via space
    a = ui.commits[0].files[0]
    assert a.folded is False
    ui._handle_key(ord(" "))   # fold back via space
    assert a.folded is True


def test_zR_unfolds_every_file_and_hunk() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("z"))
    assert ui._pending_z is True
    ui._handle_key(ord("R"))
    assert ui._pending_z is False
    c = ui.commits[0]
    for f in c.files:
        assert f.folded is False
        for h in f.hunks:
            assert h.folded is False


def test_zM_folds_everything_including_the_commit_row() -> None:
    """In multi-commit mode ``zM`` collapses the whole tree to the log line.

    The single-commit-mode behaviour of "keep the commit row open as
    orientation" no longer applies once the commit row is itself the
    log entry — a folded commit row *is* the orientation.
    """

    ui = UI([_toy_commit()])
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    ui._handle_key(ord("z"))
    ui._handle_key(ord("M"))
    c = ui.commits[0]
    assert c.folded is True
    kinds = [r.kind for r in ui.visible_rows()]
    assert kinds == ["commit"]


def test_z_followed_by_unknown_key_is_silent_noop() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("z"))
    ui._handle_key(ord("j"))
    assert ui._pending_z is False
    # ``j`` was swallowed by the pending prefix the same way vim eats
    # the second key — cursor must not have advanced.
    assert ui.cursor == 0


def test_star_opens_subtree_when_anything_closed() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))   # → file a.py (folded)
    ui._handle_key(ord("*"))
    c = ui.commits[0]
    a = c.files[0]
    assert a.folded is False
    for h in a.hunks:
        assert h.folded is False
    assert c.files[1].folded is True   # untouched neighbour


def test_star_closes_subtree_only_when_fully_open() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))
    ui._handle_key(ord("*"))   # fully open
    a = ui.commits[0].files[0]
    assert a.folded is False and all(not h.folded for h in a.hunks)
    ui._handle_key(ord("*"))   # everything was open → fold subtree
    assert a.folded is True
    for h in a.hunks:
        assert h.folded is True


def test_star_with_partial_state_opens_rather_than_closes() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))
    ui._handle_key(ord("*"))   # fully open
    ui.commits[0].files[0].hunks[0].folded = True   # re-fold one hunk
    ui._invalidate()
    ui._handle_key(ord("*"))
    a = ui.commits[0].files[0]
    assert a.folded is False
    assert all(not h.folded for h in a.hunks)


def test_star_on_commit_row_toggles_whole_tree() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("*"))
    c = ui.commits[0]
    assert c.folded is False
    for f in c.files:
        assert f.folded is False
        for h in f.hunks:
            assert h.folded is False
    ui._handle_key(ord("*"))
    # Commit itself is in the subtree, so it folds; that's fine —
    # the user explicitly asked for "everything below me."
    assert c.folded is True


def test_star_on_leaf_line_is_noop() -> None:
    ui = UI([_toy_commit()])
    ui._handle_key(ord("j"))
    for _ in range(4):
        ui._handle_key(ord("l"))
    assert ui.visible_rows()[ui.cursor].kind == "line"
    before = [(f.folded, [h.folded for h in f.hunks]) for f in ui.commits[0].files]
    ui._handle_key(ord("*"))
    after = [(f.folded, [h.folded for h in f.hunks]) for f in ui.commits[0].files]
    assert before == after


# -- message subtree ----------------------------------------------------


def test_no_message_row_when_body_is_empty() -> None:
    ui = UI([_toy_commit()])
    kinds = [r.kind for r in ui.visible_rows()]
    assert "message" not in kinds
    assert kinds == ["commit", "file", "file"]


def test_message_row_appears_above_files_when_body_present() -> None:
    ui = UI([_toy_commit(body="First line\n\nSecond paragraph")])
    kinds = [r.kind for r in ui.visible_rows()]
    # Message sits *between* the commit row and the first file row.
    assert kinds.index("message") == 1
    assert kinds.index("message") < kinds.index("file")


def test_message_unfolds_to_show_body_lines() -> None:
    ui = UI([_toy_commit(body="Line one\n\nLine three")])
    ui._handle_key(ord("j"))   # → message row
    ui._handle_key(ord("l"))   # unfold it
    rows = ui.visible_rows()
    msg_lines = [r.item for r in rows if r.kind == "message_line"]
    # splitlines preserves the blank line — prose should read identical
    # to ``git log``.
    assert msg_lines == ["Line one", "", "Line three"]


def test_star_on_commit_expands_message_too() -> None:
    ui = UI([_toy_commit(body="body line")])
    ui._handle_key(ord("*"))
    assert ui.commits[0].message.folded is False
    assert "message_line" in [r.kind for r in ui.visible_rows()]


def test_zR_unfolds_message_along_with_everything_else() -> None:
    ui = UI([_toy_commit(body="hello")])
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    assert ui.commits[0].message.folded is False


# -- working-tree section ----------------------------------------------


def test_working_tree_row_uses_a_distinct_format(monkeypatch) -> None:
    """The synthetic uncommitted-changes row should advertise itself.

    It must not look like a regular commit — no sha, date, or author
    — and the colour should differ from the magenta used for real
    commits so the user notices it as a different *kind* of entry.
    We patch ``_cp`` to echo its input so the test can compare which
    colour-pair *constant* each row asks for, without needing a real
    curses screen."""
    monkeypatch.setattr(ui_module, "_cp", lambda n: n)
    wt = _toy_working_tree(files=_toy_commit().files)
    wt.folded = False
    real = _toy_commit(sha="r")
    ui = UI([wt, real])
    rows = ui.visible_rows()
    wt_row = next(r for r in rows if r.kind == "commit" and r.item is wt)
    real_row = next(r for r in rows if r.kind == "commit" and r.item is real)
    wt_segments = ui._format_row(wt_row)
    real_segments = ui._format_row(real_row)
    wt_text = "".join(t for t, _ in wt_segments)
    assert "working tree" in wt_text
    assert "Uncommitted changes" in wt_text
    # No sha-like hex prefix, no author dash, no date on the WT row.
    assert real.short_sha not in wt_text
    assert "—" not in wt_text
    # Strip the shared ``A_BOLD`` bit; compare the colour portions of
    # each row's attribute. They must differ.
    wt_attr = wt_segments[0][1] & ~curses.A_BOLD
    real_attr = real_segments[0][1] & ~curses.A_BOLD
    assert wt_attr != real_attr


def test_working_tree_does_not_trigger_lazy_load(monkeypatch) -> None:
    """Pressing ``l`` on the working-tree row must not fire ``load_diff``.

    The synthetic commit is constructed already-loaded, so any call to
    the lazy loader would be both wasteful and (worse) a real
    ``git show ''`` against an empty sha."""
    seen: list[str] = []
    monkeypatch.setattr(
        ui_module,
        "load_diff",
        lambda sha, cwd=None: seen.append(sha) or [],
    )
    wt = _toy_working_tree(files=_toy_commit().files)
    ui = UI([wt])
    ui._handle_key(ord("l"))   # unfold
    ui._handle_key(ord("h"))   # fold back
    ui._handle_key(ord("l"))   # unfold again
    assert seen == []   # never went to the loader


def test_working_tree_files_participate_in_stat_bar_scaling() -> None:
    """Working-tree files should be part of the global bar-scale max.

    Otherwise a huge pending change wouldn't dominate the scaling the
    same way a huge commit would."""
    wt = _toy_working_tree(files=[
        File(path="big.py", old_path=None, status="M", hunks=[
            Hunk(header="@@ -1 +1,1 @@",
                 old_start=1, old_count=1, new_start=1, new_count=1,
                 context="",
                 lines=[Line("+", "x")] * 300),
        ]),
    ])
    wt.folded = False
    tiny = _toy_commit(sha="t")
    tiny.files = [
        File(path="tiny.py", old_path=None, status="M", hunks=[
            Hunk(header="@@ -1 +1,1 @@",
                 old_start=1, old_count=1, new_start=1, new_count=1,
                 context="",
                 lines=[Line("+", "x")]),
        ]),
    ]
    ui = UI([wt, tiny])
    # The big working-tree file should be in the scaling input, so
    # the tiny commit's bar shrinks to one char.
    tiny_row = next(
        r for r in ui.visible_rows()
        if r.kind == "file" and r.item.path == "tiny.py"
    )
    bar_chars = "".join(
        t for t, _ in ui._format_row(tiny_row)
        if t and set(t) <= {"+", "-"}
    )
    assert len(bar_chars) == 1


# -- multi-commit log view ---------------------------------------------


def test_multi_commit_initial_view_is_one_row_per_commit() -> None:
    """With several commits, the log view should be one folded row each."""
    commits = [
        _stub_metadata_commit("a", subject="first"),
        _stub_metadata_commit("b", subject="second"),
        _stub_metadata_commit("c", subject="third"),
    ]
    ui = UI(commits)
    kinds = [r.kind for r in ui.visible_rows()]
    assert kinds == ["commit", "commit", "commit"]


def test_J_jumps_between_commits_in_log_view() -> None:
    commits = [
        _stub_metadata_commit("a"),
        _stub_metadata_commit("b"),
        _stub_metadata_commit("c"),
    ]
    ui = UI(commits)
    ui._handle_key(ord("J"))
    assert ui.cursor == 1
    ui._handle_key(ord("J"))
    assert ui.cursor == 2
    ui._handle_key(ord("K"))
    assert ui.cursor == 1


def test_zM_collapses_all_commits_in_log() -> None:
    commits = [_toy_commit(sha="a"), _toy_commit(sha="b")]
    # ``_toy_commit`` opens its own commit; zM with the cursor on a
    # commit row should collapse everything down to one row per
    # commit.
    ui = UI(commits)
    ui._handle_key(ord("z"))
    ui._handle_key(ord("M"))
    kinds = [r.kind for r in ui.visible_rows()]
    assert kinds == ["commit", "commit"]
    assert all(c.folded for c in ui.commits)


def test_zM_on_indented_row_is_scoped_to_current_commit() -> None:
    """``zM`` from a hunk should fold only that commit's subtree.

    Other commits' fold state is left untouched, and the cursor lands
    on the parent commit row since the hunk it was sitting on is now
    hidden by the collapsed file."""
    a = _toy_commit(sha="a")
    b = _toy_commit(sha="b")
    ui = UI([a, b])
    # ``UI.__init__`` re-folds every file/hunk, so we have to open
    # them again *after* construction to set up the "everything
    # unfolded across two commits" scenario.
    for c in (a, b):
        for f in c.files:
            f.folded = False
            for h in f.hunks:
                h.folded = False
    ui._invalidate()
    # Drive the cursor onto the first hunk under commit ``a``.
    rows = ui.visible_rows()
    hunk_idx = next(i for i, r in enumerate(rows) if r.kind == "hunk")
    ui.cursor = hunk_idx
    ui._handle_key(ord("z"))
    ui._handle_key(ord("M"))
    # Commit ``a`` had its files/hunks folded; commit row stayed open
    # because the cursor was sitting inside it.
    assert a.folded is False
    assert all(f.folded for f in a.files)
    assert all(h.folded for f in a.files for h in f.hunks)
    # Commit ``b`` is completely untouched.
    assert b.folded is False
    assert b.files[0].folded is False
    assert b.files[0].hunks[0].folded is False
    assert b.files[1].folded is False
    # Cursor landed on commit ``a``'s row.
    assert ui.visible_rows()[ui.cursor].kind == "commit"
    assert ui.visible_rows()[ui.cursor].item is a


def test_zR_on_indented_row_unfolds_only_current_commit() -> None:
    """``zR`` from inside one commit shouldn't drag others open."""
    a = _stub_metadata_commit("a")
    b = _stub_metadata_commit("b")
    # Pre-load and pre-open commit ``a`` so we can put the cursor on a
    # file row inside it, but leave ``b`` collapsed and unloaded.
    a.files = _toy_commit().files
    a._loaded = True
    a.folded = False
    ui = UI([a, b])
    rows = ui.visible_rows()
    file_idx = next(i for i, r in enumerate(rows) if r.kind == "file")
    ui.cursor = file_idx
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    # Inside commit ``a`` everything is open now.
    for f in a.files:
        assert f.folded is False
        for h in f.hunks:
            assert h.folded is False
    # Commit ``b`` was never loaded and never unfolded.
    assert b._loaded is False
    assert b.folded is True


def test_zR_on_indented_row_keeps_cursor_on_same_item() -> None:
    """Identity-tracking: the cursor should follow the same row item

    after ``zR`` even though new rows are inserted above and below it."""
    c = _toy_commit(body="hi")
    ui = UI([c])
    # Drop the cursor onto the second file (which is still folded), so
    # there's only one row between it and the commit row.
    rows = ui.visible_rows()
    second_file_idx = [i for i, r in enumerate(rows) if r.kind == "file"][1]
    ui.cursor = second_file_idx
    second_file = rows[second_file_idx].item
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    # After zR, the message row and the first file's hunks (and lines)
    # are now visible above us — the cursor index *must* have shifted
    # to compensate, but the actual file object under the cursor
    # should be the same.
    assert ui.visible_rows()[ui.cursor].item is second_file


# -- stat bar ----------------------------------------------------------


def _flatten_segments(segments) -> str:
    return "".join(text for text, _ in segments)


def test_stat_bar_one_to_one_for_small_commits() -> None:
    """When the largest file has fewer changes than the bar width,

    each file should get exactly one bar character per change. Mirrors
    ``git diff --stat`` behaviour for small diffs."""
    ui = UI([_toy_commit()])
    f = ui.commits[0].files[0]   # ``a.py`` has 2 additions, 2 deletions
    n_add, n_del = ui._stat_bar_widths(f, max_total=4)
    assert (n_add, n_del) == (2, 2)


def test_stat_bar_scales_when_max_exceeds_width() -> None:
    """A file with 100 changes in a commit whose max is 200 should fill

    half the bar width, with the add/del ratio preserved."""
    ui = UI([_toy_commit()])
    f = File(
        path="big.py",
        old_path=None,
        status="M",
        hunks=[],
    )
    # Hand-rigged additions/deletions via a fake hunk so the
    # ``additions`` / ``deletions`` properties report what we want.
    f.hunks = [
        Hunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            context="",
            lines=[Line("+", "x")] * 80 + [Line("-", "y")] * 20,
        ),
    ]
    n_add, n_del = ui._stat_bar_widths(f, max_total=200, width=16)
    # 80/200 * 16 = 6.4 → 6 pluses; 20/200 * 16 = 1.6 → 2 minuses
    assert n_add == 6
    assert n_del == 2


def test_stat_bar_gives_one_char_minimum_for_nonzero_side() -> None:
    """A single deletion in a huge commit should still show as ``-``,

    not vanish to zero from rounding."""
    ui = UI([_toy_commit()])
    f = File(path="tiny.py", old_path=None, status="M", hunks=[])
    f.hunks = [
        Hunk(
            header="@@ -1 +0,0 @@",
            old_start=1,
            old_count=1,
            new_start=0,
            new_count=0,
            context="",
            lines=[Line("-", "x")],
        ),
    ]
    # Max in the commit is 1000 — naive rounding would give 0 minuses.
    n_add, n_del = ui._stat_bar_widths(f, max_total=1000, width=16)
    assert n_add == 0
    assert n_del == 1


def test_stat_bar_empty_for_zero_change_file() -> None:
    """A mode-only change (no +/- lines) should produce no bar."""
    ui = UI([_toy_commit()])
    f = File(path="modeonly.py", old_path=None, status="M", hunks=[])
    n_add, n_del = ui._stat_bar_widths(f, max_total=5)
    assert (n_add, n_del) == (0, 0)


def test_file_row_bar_has_right_number_of_pluses_and_minuses() -> None:
    """The trailing bar should contain one ``+`` per addition and one

    ``-`` per deletion (when below the scaling threshold)."""
    ui = UI([_toy_commit()])
    file_row = next(r for r in ui.visible_rows() if r.kind == "file")
    segments = ui._format_row(file_row)
    # Bar segments are the trailing ones whose text is purely ``+`` or
    # ``-`` characters — the count segments contain digits too, so
    # ``set(text) == {"+"}`` distinguishes them cleanly.
    bar_pluses = "".join(t for t, _ in segments if t and set(t) == {"+"})
    bar_minuses = "".join(t for t, _ in segments if t and set(t) == {"-"})
    f = ui.commits[0].files[0]
    assert len(bar_pluses) == f.additions
    assert len(bar_minuses) == f.deletions


def test_file_row_counts_share_color_with_matching_bar_sigil() -> None:
    """``+N`` should be the same color as the bar's ``+`` characters,

    and ``-M`` the same as the bar's ``-`` characters. That mirrors
    the colouring of ``+`` / ``-`` sigils on diff body lines."""
    ui = UI([_toy_commit()])
    file_row = next(r for r in ui.visible_rows() if r.kind == "file")
    segments = ui._format_row(file_row)
    # The count segments are the ones that contain both a sign and a
    # digit; the bar segments are pure sigil runs.
    add_count = next(
        s for s in segments
        if s[0].startswith("+") and any(c.isdigit() for c in s[0])
    )
    del_count = next(
        s for s in segments
        if s[0].startswith("-") and any(c.isdigit() for c in s[0])
    )
    bar_add = next(s for s in segments if s[0] and set(s[0]) == {"+"})
    bar_del = next(s for s in segments if s[0] and set(s[0]) == {"-"})
    assert add_count[1] == bar_add[1]
    assert del_count[1] == bar_del[1]


def test_binary_file_row_has_no_stat_bar() -> None:
    """Binary files should render only as ``[B] path  (binary)``

    — no counts, no bar."""
    c = _toy_commit()
    c.files = [
        File(path="logo.png", old_path=None, status="B", hunks=[], binary=True),
    ]
    ui = UI([c])
    file_row = next(r for r in ui.visible_rows() if r.kind == "file")
    segments = ui._format_row(file_row)
    # No bar segments (pure +/- runs) and no digit-bearing count
    # segments — only the prefix and the literal ``(binary)`` label.
    flat = _flatten_segments(segments)
    assert "(binary)" in flat
    assert "+" not in flat and "-" not in flat
    assert "[B]" in flat and "logo.png" in flat


def test_file_row_stat_bars_align_across_different_path_widths() -> None:
    """The point of padding: bars from short and long paths should

    start at the same column so the user can compare them visually."""
    c = _toy_commit()
    short = File(
        path="a.py",
        old_path=None,
        status="M",
        hunks=[
            Hunk(
                header="@@ -1,1 +1,1 @@",
                old_start=1,
                old_count=1,
                new_start=1,
                new_count=1,
                context="",
                lines=[Line("-", "old"), Line("+", "new")],
            ),
        ],
    )
    longer = File(
        path="much/longer/path/name.py",
        old_path=None,
        status="M",
        hunks=[
            Hunk(
                header="@@ -1,1 +1,1 @@",
                old_start=1,
                old_count=1,
                new_start=1,
                new_count=1,
                context="",
                lines=[Line("-", "old"), Line("+", "new")],
            ),
        ],
    )
    c.files = [short, longer]
    ui = UI([c])

    def bar_start_col(row) -> int:
        col = 0
        for text, _ in ui._format_row(row):
            if text and set(text) == {"+"}:
                return col
            col += len(text)
        return col

    file_rows = [r for r in ui.visible_rows() if r.kind == "file"]
    cols = [bar_start_col(r) for r in file_rows]
    assert len(set(cols)) == 1, f"bars start at different columns: {cols}"


def test_stat_bars_scale_across_open_commits_not_just_within() -> None:
    """The largest file across *all* open commits should fill the bar.

    A small commit opened alongside a big one should see its bars
    truncated, because the global scale is set by the big commit's
    largest file."""
    # Tiny commit: one file, 1 addition + 0 deletions.
    tiny = _toy_commit(sha="t")
    tiny.files = [
        File(path="tiny.py", old_path=None, status="M", hunks=[
            Hunk(header="@@ -1 +1,1 @@",
                 old_start=1, old_count=1, new_start=1, new_count=1,
                 context="",
                 lines=[Line("+", "x")]),
        ]),
    ]
    # Big commit: one file, lots of changes — large enough that the
    # scaling kicks in past the 1:1 threshold (16 chars).
    big = _toy_commit(sha="b")
    big.files = [
        File(path="big.py", old_path=None, status="M", hunks=[
            Hunk(header="@@ -1 +1,1 @@",
                 old_start=1, old_count=1, new_start=1, new_count=1,
                 context="",
                 lines=[Line("+", "x")] * 200),
        ]),
    ]
    ui = UI([tiny, big])

    def bar_len(file_row) -> int:
        segs = ui._format_row(file_row)
        return sum(len(t) for t, _ in segs if t and set(t) <= {"+", "-"} and t)

    file_rows = [r for r in ui.visible_rows() if r.kind == "file"]
    # The big file fills the bar (16 chars); the tiny file gets just
    # one ``+`` because its one change scales to almost nothing
    # against 200 — but it must not vanish, by the "≥1 if non-zero"
    # rule baked into ``_stat_bar_widths``.
    big_row = next(r for r in file_rows if r.item.path == "big.py")
    tiny_row = next(r for r in file_rows if r.item.path == "tiny.py")
    assert bar_len(big_row) == 16
    assert bar_len(tiny_row) == 1


def test_closing_a_commit_rescales_remaining_bars() -> None:
    """Closing the big commit should free the tiny one's bars to use

    the full 1:1 mapping again, since the new global max drops back
    below the bar-width threshold."""
    tiny = _toy_commit(sha="t")
    tiny.files = [
        File(path="tiny.py", old_path=None, status="M", hunks=[
            Hunk(header="@@ -1 +1,3 @@",
                 old_start=1, old_count=1, new_start=1, new_count=3,
                 context="",
                 lines=[Line("+", "x"), Line("+", "y"), Line("+", "z")]),
        ]),
    ]
    big = _toy_commit(sha="b")
    big.files = [
        File(path="big.py", old_path=None, status="M", hunks=[
            Hunk(header="@@ -1 +1,1 @@",
                 old_start=1, old_count=1, new_start=1, new_count=1,
                 context="",
                 lines=[Line("+", "x")] * 200),
        ]),
    ]
    ui = UI([tiny, big])

    def tiny_bar_len() -> int:
        row = next(
            r for r in ui.visible_rows()
            if r.kind == "file" and r.item.path == "tiny.py"
        )
        segs = ui._format_row(row)
        return sum(len(t) for t, _ in segs if t and set(t) <= {"+", "-"} and t)

    # While big is open, tiny gets one char per side (scaled down).
    assert tiny_bar_len() < 3
    # Close the big commit and tiny gets the full 1:1 (3 chars).
    big.folded = True
    ui._invalidate()
    assert tiny_bar_len() == 3


def test_file_row_counts_align_across_different_count_widths() -> None:
    """Files with single- vs. multi-digit counts should both have the

    bar at the same column — the counts column is padded to its widest
    visible entry."""
    c = _toy_commit()
    small = File(path="a.py", old_path=None, status="M", hunks=[
        Hunk(header="@@ -1,1 +1,1 @@",
             old_start=1, old_count=1, new_start=1, new_count=1,
             context="",
             lines=[Line("+", "x")]),
    ])
    big = File(path="b.py", old_path=None, status="M", hunks=[
        Hunk(header="@@ -1,1 +1,1 @@",
             old_start=1, old_count=1, new_start=1, new_count=1,
             context="",
             lines=[Line("+", "x")] * 100 + [Line("-", "y")] * 100),
    ])
    c.files = [small, big]
    ui = UI([c])

    def bar_start_col(row) -> int:
        col = 0
        for text, _ in ui._format_row(row):
            if text and set(text) == {"+"}:
                return col
            col += len(text)
        return col

    file_rows = [r for r in ui.visible_rows() if r.kind == "file"]
    cols = [bar_start_col(r) for r in file_rows]
    assert len(set(cols)) == 1, f"bars start at different columns: {cols}"


# -- lazy loading -------------------------------------------------------


def _install_fake_loader(monkeypatch, loader) -> list[str]:
    """Replace ``load_diff`` and record which SHAs it was asked for."""
    seen: list[str] = []

    def wrapper(sha, cwd=None):
        seen.append(sha)
        return loader(sha)

    monkeypatch.setattr(ui_module, "load_diff", wrapper)
    return seen


def test_l_on_unloaded_commit_triggers_diff_load(monkeypatch) -> None:
    """First ``l`` press on a folded commit row should fetch its diff."""
    seen = _install_fake_loader(monkeypatch, lambda sha: [])
    commit = _stub_metadata_commit("a")
    ui = UI([commit])
    assert commit._loaded is False
    ui._handle_key(ord("l"))
    assert commit._loaded is True
    assert seen == [commit.sha]


def test_load_is_idempotent(monkeypatch) -> None:
    """A second ``l`` press should not re-issue the diff fetch."""
    seen = _install_fake_loader(monkeypatch, lambda sha: [])
    commit = _stub_metadata_commit("a")
    ui = UI([commit])
    ui._handle_key(ord("l"))   # loads + unfolds
    ui._handle_key(ord("h"))   # folds back
    ui._handle_key(ord("l"))   # unfolds again
    assert seen == [commit.sha]   # only one fetch total


def test_zR_loads_every_commit_in_the_log(monkeypatch) -> None:
    """``zR`` is an explicit "give me everything" — including diff loads."""
    seen = _install_fake_loader(monkeypatch, lambda sha: [])
    commits = [
        _stub_metadata_commit("a"),
        _stub_metadata_commit("b"),
        _stub_metadata_commit("c"),
    ]
    ui = UI(commits)
    ui._handle_key(ord("z"))
    ui._handle_key(ord("R"))
    assert set(seen) == {c.sha for c in commits}
    for c in commits:
        assert c._loaded is True
        assert c.folded is False


def test_load_failure_is_swallowed(monkeypatch) -> None:
    """A GitError during lazy load should not crash the UI."""
    from gitpeek.git import GitError

    def boom(sha):
        raise GitError("simulated failure")

    _install_fake_loader(monkeypatch, boom)
    commit = _stub_metadata_commit("a")
    ui = UI([commit])
    ui._handle_key(ord("l"))   # should not raise
    assert commit._loaded is True
    assert commit.files == []
