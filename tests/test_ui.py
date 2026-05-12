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
    # ``_toy_commit`` opens its own commit; zM should still collapse
    # everything down to one row per commit.
    ui = UI(commits)
    ui._handle_key(ord("z"))
    ui._handle_key(ord("M"))
    kinds = [r.kind for r in ui.visible_rows()]
    assert kinds == ["commit", "commit"]
    assert all(c.folded for c in ui.commits)


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
