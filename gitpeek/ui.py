"""Curses UI for gitpeek.

The screen layout matches ``hg commit -i``'s crecord selector closely:

    +----------------------------------------------------------+
    | two-line status/help bar (bold)                          |
    +----------------------------------------------------------+
    | scrollable tree of commit / files / hunks / lines        |
    | current row is highlighted in reverse video              |
    +----------------------------------------------------------+

The tree is rendered from a flat ``visible_rows`` list that's rebuilt
on every keypress from the fold flags on each node. That makes scroll
math, cursor math, and "jump to next sibling" all trivial — they're
just indexing into a list.

Keybindings are deliberately the same letters as crecord, even where
the semantics are read-only-shifted (space is a no-op, c/r/A/e are
absent).
"""

from __future__ import annotations

import curses
import locale
from dataclasses import dataclass
from typing import Any

from gitpeek.diff import Commit, File, Hunk, Line, Message
from gitpeek.git import GitError, load_diff


# Color pair indices. ``curses.init_pair`` uses 1-based indexing and 0
# is reserved for the terminal default, so we start at 1.
_CP_ADD = 1       # '+' lines
_CP_DEL = 2       # '-' lines
_CP_HUNK = 3      # @@ hunk headers
_CP_FILE = 4      # file rows
_CP_COMMIT = 5    # commit row
_CP_STATUS = 6    # top status bar
_CP_DIM = 7       # secondary text (author/date, etc.)


# Indentation per nesting level. Two spaces matches crecord's feel
# without eating too much horizontal space on a narrow terminal.
_INDENT = "  "


# Glyphs for the fold state. crecord uses ``**`` for folded; we use a
# right-pointing triangle when folded and a down-pointing one when
# unfolded, which reads more obviously as "click to expand" while still
# staying in the same visual family.
_GLYPH_OPEN = "▼"
_GLYPH_CLOSED = "▶"


# Max width of the ``git --stat``-style bar (``+++--``) after each
# file row. Sixteen characters keeps the bar visible on the typical
# 80-column terminal without crowding out long path names.
_STAT_BAR_WIDTH = 16


# Raw key codes for the ``Ctrl-<x>`` combos we recognise. curses
# delivers these as plain integers (the ASCII control-character values),
# not via ``KEY_*`` constants, so we just name them ourselves.
_CTRL_D = 0x04   # half-page down (vim)
_CTRL_U = 0x15   # half-page up   (vim)


def _cp(n: int) -> int:
    """``curses.color_pair`` that's safe to call before ``initscr``.

    Outside :func:`curses.wrapper` (i.e., in unit tests that drive the
    formatting code directly) ``curses.color_pair`` raises because no
    color table has been initialised. Return zero — the terminal's
    default attribute — so the row-formatting logic stays exercisable
    without spinning up a real screen.
    """

    try:
        return curses.color_pair(n)
    except curses.error:
        return 0


@dataclass
class Row:
    """One renderable line in the flattened tree view.

    ``item`` is the underlying ``Commit`` / ``Message`` / ``File`` /
    ``Hunk`` / ``Line`` node; ``depth`` is its nesting level (used both
    for indentation and for "jump to parent"); ``kind`` is a short
    string used by the "next/prev of same kind" navigation;
    ``parent_commit`` is the enclosing :class:`Commit` for every non-
    commit row (and ``None`` for commit rows themselves), so the UI
    can answer "which commit am I in?" without walking the row list.
    """

    item: Any
    depth: int
    kind: str  # 'commit' | 'message' | 'message_line' | 'file' | 'hunk' | 'line'
    parent_commit: Commit | None = None


class UI:
    """Stateful curses application — instantiate once, call :meth:`run`.

    The constructor takes the full list of commits (typically
    :func:`gitpeek.git.load_log`) and the working directory the log
    came from, so the UI can fetch each commit's diff on demand. Every
    commit and every already-loaded subtree starts folded — the user
    is looking at a log, not a wall of diffs — and depth-by-depth
    navigation reveals exactly one level per ``l`` press.
    """

    def __init__(self, commits: list[Commit], cwd: str | None = None) -> None:
        self.commits = commits
        self.cwd = cwd
        # Force a depth-by-depth start state on any *loaded* subtree:
        # every file and hunk begins folded so each ``l`` press reveals
        # exactly one more level. We deliberately leave ``commit.folded``
        # alone — its dataclass default is True (log-view friendly),
        # but a caller that wants a particular commit pre-opened
        # (tests, ``gitpeek -n 1``-style flows) can pass it in
        # ``folded=False`` and we'll respect that.
        for c in commits:
            for f in c.files:
                f.folded = True
                for h in f.hunks:
                    h.folded = True
        self.cursor = 0
        self.scroll = 0
        self.help_visible = False
        self._cached_rows: list[Row] | None = None
        # Height of the scrollable content area at last render. Used
        # by ``Ctrl-D`` / ``Ctrl-U`` to size their jump by half the
        # current screen. Defaults to something reasonable so the
        # first keypress before any render still does something
        # useful instead of jumping by zero.
        self._last_content_h = 20
        # Widths used to align the stat bar across visible file rows.
        # Recomputed every time the visible-rows list is rebuilt so the
        # left edge of every bar lines up at one column — same idea as
        # ``git diff --stat`` padding paths to a common width.
        self._file_row_max_prefix = 0
        self._file_row_max_counts = 0
        # Largest ``additions + deletions`` among visible file rows.
        # Used as the scaling input for stat bars, so a file's bar
        # length is proportional to every other *open* file in the log
        # rather than only its commit-siblings. This makes bars
        # comparable across commits at the cost of churn: opening or
        # closing a commit can change the max and therefore every bar.
        self._file_row_max_changes = 0
        # vim-style ``z`` prefix: when True, the next keypress is
        # interpreted as part of a ``z<x>`` sequence (currently ``zM``
        # and ``zR``). Cleared on the next keystroke regardless of
        # whether it matched a known sequence — matches vim's
        # behaviour and means a stray arrow after ``z`` doesn't both
        # cancel the prefix *and* move the cursor.
        self._pending_z = False

    # -- model ----------------------------------------------------------

    def _invalidate(self) -> None:
        """Drop the cached visible-rows list after a fold-state change."""
        self._cached_rows = None

    def visible_rows(self) -> list[Row]:
        """Flatten the tree into a list of rows respecting fold flags.

        Cached between keypresses; invalidated whenever fold flags
        change. Rebuilding from scratch is O(n) and fine for any commit
        a human will look at, but caching avoids doing it three times
        per keypress (once for handling, twice for rendering).
        """

        if self._cached_rows is not None:
            return self._cached_rows
        rows: list[Row] = []
        for commit in self.commits:
            rows.append(Row(commit, 0, "commit"))
            if commit.folded:
                continue
            # Message comes before files so the natural top-to-bottom
            # order under a commit is "why" then "what" — same shape
            # as a ``git log -p`` page.
            msg = commit.message
            if msg.lines:
                rows.append(Row(msg, 1, "message", parent_commit=commit))
                if not msg.folded:
                    for line in msg.lines:
                        rows.append(Row(line, 2, "message_line", parent_commit=commit))
            for f in commit.files:
                rows.append(Row(f, 1, "file", parent_commit=commit))
                if not f.folded:
                    for h in f.hunks:
                        rows.append(Row(h, 2, "hunk", parent_commit=commit))
                        if not h.folded:
                            for ln in h.lines:
                                rows.append(Row(ln, 3, "line", parent_commit=commit))
        self._recompute_file_row_widths(rows)
        self._cached_rows = rows
        return rows

    def _recompute_file_row_widths(self, rows: list[Row]) -> None:
        """Find the widest prefix and counts string among visible file rows.

        Used by :meth:`_format_row` to right-pad each file's prefix and
        counts so every stat bar starts at the same column. Recomputed
        whenever the visible-rows list is rebuilt — that's the only
        moment when the set of visible files (and therefore the
        max-width inputs) can change.
        """

        max_prefix = 0
        max_counts = 0
        max_changes = 0
        for r in rows:
            if r.kind != "file":
                continue
            f: File = r.item
            indent = _INDENT * r.depth
            path = f.path
            if f.old_path and f.old_path != f.path:
                path = f"{f.old_path} → {f.path}"
            # Glyph width is one cell whether folded or open, so the
            # prefix's printed length doesn't depend on fold state.
            prefix_len = len(f"{indent}{_GLYPH_OPEN} [{f.status}] {path}")
            max_prefix = max(max_prefix, prefix_len)
            if not f.binary:
                # ``+N -M`` — three pieces (plus, space, minus). Binary
                # files don't contribute because they show "(binary)"
                # in place of the counts.
                counts_len = (
                    len(f"+{f.additions}") + 1 + len(f"-{f.deletions}")
                )
                max_counts = max(max_counts, counts_len)
                max_changes = max(max_changes, f.additions + f.deletions)
        self._file_row_max_prefix = max_prefix
        self._file_row_max_counts = max_counts
        self._file_row_max_changes = max_changes

    def _has_children(self, row: Row) -> bool:
        """True if this row owns at least one child node."""
        item = row.item
        if isinstance(item, Commit):
            # Before lazy-loading we can't *know* whether a commit has
            # any files — only its message-presence is visible from the
            # metadata pass. Treat unloaded commits as "potentially has
            # children" so ``l`` will attempt the load; once loaded we
            # use the actual file/message presence so navigation around
            # truly empty commits (root commit with no parents, empty
            # merge) stops behaving as if there's something to open.
            if not item._loaded:
                return True
            return bool(item.files) or bool(item.message.lines)
        if isinstance(item, Message):
            return bool(item.lines)
        if isinstance(item, File):
            return bool(item.hunks)
        if isinstance(item, Hunk):
            return bool(item.lines)
        return False

    def _ensure_loaded(self, commit: Commit) -> None:
        """Lazy-fetch ``commit``'s diff on first access.

        Idempotent — repeated calls are free. Failures (bad ref,
        permission error) are swallowed so the UI can keep running;
        the affected commit will simply appear to have no files. We
        choose silent degradation over raising into the curses loop
        because there's no good way to surface a one-off error mid-
        navigation without disrupting the whole view.
        """

        if commit._loaded:
            return
        try:
            commit.files = load_diff(commit.sha, cwd=self.cwd)
        except GitError:
            commit.files = []
        # Newly loaded files default to ``folded=False``; fold them so
        # the user's first ``l`` after the load reveals one level, not
        # the entire diff at once.
        for f in commit.files:
            f.folded = True
            for h in f.hunks:
                h.folded = True
        commit._loaded = True
        self._invalidate()

    def _collect_subtree_foldables(self, row: Row) -> list:
        """Return all foldable nodes in the subtree rooted at ``row``.

        Walks the *model* (not the visible-rows list), so it sees
        descendants regardless of their current fold state — that's
        what lets ``*`` reach into a collapsed file and flip its hidden
        hunks in one keystroke. The root node is included when it has
        children; leaves (Line) and empty branches are skipped.
        """

        item = row.item
        nodes: list = []
        if isinstance(item, Commit):
            if not (item.files or item.message.lines):
                return nodes
            nodes.append(item)
            if item.message.lines:
                nodes.append(item.message)
            for f in item.files:
                if f.hunks:
                    nodes.append(f)
                    for h in f.hunks:
                        if h.lines:
                            nodes.append(h)
        elif isinstance(item, Message):
            if item.lines:
                nodes.append(item)
        elif isinstance(item, File):
            if not item.hunks:
                return nodes
            nodes.append(item)
            for h in item.hunks:
                if h.lines:
                    nodes.append(h)
        elif isinstance(item, Hunk):
            if item.lines:
                nodes.append(item)
        return nodes

    def _toggle_fold(self, row: Row) -> None:
        """Flip the fold flag on ``row``'s item, lazy-loading if needed.

        Shared by ``f`` and ``SPACE``. Lazy-loads when we're about to
        unfold a previously-folded commit so the new state has files
        to reveal; no-op when the cursor is on a node without
        children (e.g., a line row or an empty commit).
        """

        item = row.item
        if isinstance(item, Commit) and item.folded:
            self._ensure_loaded(item)
        if hasattr(item, "folded") and self._has_children(row):
            item.folded = not item.folded
            self._invalidate()

    def _fold_tree(
        self, folded: bool, scope_commit: Commit | None = None
    ) -> None:
        """Set fold state on a tree-wide or single-commit scope.

        Used by ``zM`` / ``zR``. The scope follows the cursor:

        * ``scope_commit is None`` — every commit in the log, plus
          their messages, files, and hunks. The commit rows themselves
          are also toggled, so ``zM`` collapses the whole tree to a
          one-row-per-commit log view.
        * ``scope_commit is c`` — only ``c``'s subtree. The commit row
          stays unfolded because the cursor is sitting inside it; if
          we folded it the cursor would land in limbo.
        """

        if scope_commit is None:
            targets = self.commits
            touch_commit_flag = True
        else:
            targets = [scope_commit]
            touch_commit_flag = False

        for commit in targets:
            if not folded:
                # ``zR`` means "open everything." For commits we
                # haven't seen yet, that requires fetching the diff so
                # the unfold has something to reveal. Loading every
                # commit in scope is the user's explicit ask — they
                # pressed the "give me all the things" key — and they
                # can ``zM`` back if it turns out to be too much.
                self._ensure_loaded(commit)
            if touch_commit_flag:
                commit.folded = folded
            if commit.message.lines:
                commit.message.folded = folded
            for f in commit.files:
                if f.hunks:
                    f.folded = folded
                for h in f.hunks:
                    if h.lines:
                        h.folded = folded
        self._invalidate()
        new_rows = self.visible_rows()
        if self.cursor >= len(new_rows):
            self.cursor = len(new_rows) - 1

    # -- run loop -------------------------------------------------------

    def run(self, stdscr: "curses.window") -> None:
        """Main event loop. Called by :func:`curses.wrapper`."""

        curses.curs_set(0)
        # Honour the terminal's existing palette where possible — looks
        # better against custom color schemes than hard-coding a BG.
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(_CP_ADD, curses.COLOR_GREEN, bg)
        curses.init_pair(_CP_DEL, curses.COLOR_RED, bg)
        curses.init_pair(_CP_HUNK, curses.COLOR_CYAN, bg)
        curses.init_pair(_CP_FILE, curses.COLOR_YELLOW, bg)
        curses.init_pair(_CP_COMMIT, curses.COLOR_MAGENTA, bg)
        curses.init_pair(_CP_STATUS, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(_CP_DIM, curses.COLOR_WHITE, bg)

        # Enable mouse wheel etc. only if the terminal supports it.
        try:
            stdscr.keypad(True)
        except curses.error:
            pass

        while True:
            self._render(stdscr)
            ch = stdscr.getch()
            if not self._handle_key(ch):
                return

    # -- key handling ---------------------------------------------------

    def _handle_key(self, ch: int) -> bool:
        """Process a single keypress. Returns False to exit the loop."""

        # Help is modal: any key dismisses it, even quit. This matches
        # crecord, where ``?`` is a toggle and the next keypress goes to
        # the underlying screen.
        if self.help_visible:
            self.help_visible = False
            return True

        # vim-style ``z<x>`` prefix sequences. Resolved before anything
        # else so a pending ``z`` consumes exactly one follow-up key,
        # match or no match.
        if self._pending_z:
            self._pending_z = False
            # The cursor's row decides the scope: on a commit row,
            # ``z<x>`` spans every commit (the "everything" gesture);
            # on any indented row, it stays inside the commit the
            # cursor is currently inside. ``visible_rows`` always
            # leaves the cursor on a valid row, so ``rows[cursor]``
            # is safe to read here even before the fold modifies the
            # list.
            rows = self.visible_rows()
            row = rows[self.cursor]
            if ch == ord("M"):
                if row.kind == "commit":
                    self._fold_tree(True)
                else:
                    # Collapsing inside a commit makes the cursor's
                    # current row (file/hunk/line/message_line)
                    # disappear. Land on the parent commit row so the
                    # user stays oriented at the smallest still-visible
                    # ancestor.
                    commit = row.parent_commit
                    self._fold_tree(True, scope_commit=commit)
                    if commit is not None:
                        new_rows = self.visible_rows()
                        for i, r in enumerate(new_rows):
                            if r.kind == "commit" and r.item is commit:
                                self.cursor = i
                                break
            elif ch == ord("R"):
                cursor_item = row.item
                if row.kind == "commit":
                    self._fold_tree(False)
                else:
                    self._fold_tree(False, scope_commit=row.parent_commit)
                # zR only *adds* rows around the cursor; identity-
                # track the original cursor item so its index follows
                # the new row positions.
                new_rows = self.visible_rows()
                for i, r in enumerate(new_rows):
                    if r.item is cursor_item:
                        self.cursor = i
                        break
            # Unknown ``z<x>`` — swallow silently rather than letting
            # the second key double as both "cancel the prefix" and
            # its own action. Same as vim.
            return True

        rows = self.visible_rows()
        n = len(rows)
        row = rows[self.cursor]

        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("?"):
            self.help_visible = True
            return True
        if ch == ord("z"):
            self._pending_z = True
            return True

        if ch in (ord("j"), curses.KEY_DOWN):
            self.cursor = min(self.cursor + 1, n - 1)
        elif ch in (ord("k"), curses.KEY_UP):
            self.cursor = max(self.cursor - 1, 0)
        elif ch == ord("g"):
            self.cursor = 0
        elif ch == ord("G"):
            self.cursor = n - 1
        elif ch == _CTRL_D:
            # Vim-style half-page jump: ``content_h // 2`` rows, with
            # a one-row minimum so on tiny terminals the key still
            # moves something. The render-time scroll logic keeps the
            # cursor visible afterwards, so we don't need to touch
            # ``self.scroll`` here.
            step = max(1, self._last_content_h // 2)
            self.cursor = min(self.cursor + step, n - 1)
        elif ch == _CTRL_U:
            step = max(1, self._last_content_h // 2)
            self.cursor = max(self.cursor - step, 0)

        elif ch in (ord("l"), curses.KEY_RIGHT):
            # crecord semantics: if the current node is folded, unfold
            # it; otherwise step into the first child. The two-step
            # behaviour is what makes l/h feel like a directional tree
            # walk instead of a fold toggle.
            item = row.item
            # Pull the diff on first unfold so ``_has_children`` knows
            # whether there's anything to reveal. No-op for already-
            # loaded commits and for non-Commit nodes.
            if isinstance(item, Commit) and item.folded:
                self._ensure_loaded(item)
            if hasattr(item, "folded") and self._has_children(row):
                if item.folded:
                    item.folded = False
                    self._invalidate()
                elif self.cursor + 1 < n and rows[self.cursor + 1].depth > row.depth:
                    self.cursor += 1

        elif ch in (ord("h"), curses.KEY_LEFT):
            # Mirror of ``l``: collapse if we're an unfolded parent;
            # otherwise jump up to our parent row.
            item = row.item
            if (
                hasattr(item, "folded")
                and not item.folded
                and self._has_children(row)
            ):
                item.folded = True
                self._invalidate()
            else:
                for i in range(self.cursor - 1, -1, -1):
                    if rows[i].depth < row.depth:
                        self.cursor = i
                        break

        elif ch == ord("f"):
            # Plain toggle. ``l`` / ``h`` are directional; ``f`` is the
            # "I just want to flip this regardless of where the cursor
            # ends up" key.
            self._toggle_fold(row)

        elif ch == ord("F"):
            # Fold current + ancestors. Useful for getting back to a
            # clean "just the commit row, please" view from deep inside
            # a hunk without tapping ``h`` repeatedly.
            item = row.item
            if hasattr(item, "folded") and self._has_children(row):
                item.folded = True
            depth = row.depth
            for i in range(self.cursor - 1, -1, -1):
                if rows[i].depth < depth:
                    ancestor = rows[i].item
                    if hasattr(ancestor, "folded") and self._has_children(rows[i]):
                        ancestor.folded = True
                    depth = rows[i].depth
                    if depth == 0:
                        break
            self._invalidate()
            # After folding, the cursor may now point past the end of
            # the (smaller) visible list — clamp it.
            new_rows = self.visible_rows()
            if self.cursor >= len(new_rows):
                self.cursor = len(new_rows) - 1

        elif ch in (ord("J"), curses.KEY_NPAGE):
            # Next sibling of the same kind. crecord's J skips over a
            # whole expanded hunk in one keystroke; we do the same by
            # advancing to the next row whose ``kind`` matches.
            for i in range(self.cursor + 1, n):
                if rows[i].kind == row.kind:
                    self.cursor = i
                    break
        elif ch in (ord("K"), curses.KEY_PPAGE):
            for i in range(self.cursor - 1, -1, -1):
                if rows[i].kind == row.kind:
                    self.cursor = i
                    break

        elif ch == ord("*"):
            # Smart subtree toggle: if *anything* under (or at) the
            # cursor is folded, unfold everything; only when the whole
            # subtree is already open do we fold it. This makes ``*``
            # idempotent toward "fully open" in two keypresses no
            # matter what state you start from.
            # Lazy-load if the cursor is sitting on an unloaded commit
            # — otherwise the "any closed" check would only see the
            # commit's own fold flag and miss the (yet-to-be-fetched)
            # files entirely.
            if isinstance(row.item, Commit):
                self._ensure_loaded(row.item)
            nodes = self._collect_subtree_foldables(row)
            if nodes:
                any_closed = any(n.folded for n in nodes)
                new_state = not any_closed
                for n in nodes:
                    n.folded = new_state
                self._invalidate()
                new_rows = self.visible_rows()
                if self.cursor >= len(new_rows):
                    self.cursor = len(new_rows) - 1

        elif ch == ord(" "):
            # Space is the easy one-finger fold toggle — same effect
            # as ``f``. crecord uses space for (un-)select, but we
            # have no selection model, so this is the most useful
            # thing to bind it to without breaking muscle memory.
            self._toggle_fold(row)

        elif ch == curses.KEY_RESIZE:
            # Curses delivers a synthetic key when the terminal is
            # resized. The next render reads ``getmaxyx`` fresh, so we
            # just need to make sure the cursor is still on-screen.
            pass

        return True

    # -- rendering ------------------------------------------------------

    def _render(self, stdscr: "curses.window") -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 4 or w < 20:
            # Too small to draw anything sensible; bail without crashing.
            try:
                stdscr.addnstr(0, 0, "terminal too small", w - 1)
            except curses.error:
                pass
            stdscr.refresh()
            return

        self._draw_status_bar(stdscr, w)

        content_y = 2
        content_h = h - content_y
        # Stash for ``Ctrl-D`` / ``Ctrl-U`` which run between renders
        # and otherwise have no way to learn the screen height.
        self._last_content_h = content_h
        rows = self.visible_rows()

        # Keep the cursor on-screen. Scroll only as much as needed.
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + content_h:
            self.scroll = self.cursor - content_h + 1
        # Don't leave a gap at the bottom when the list is short.
        max_scroll = max(0, len(rows) - content_h)
        self.scroll = min(self.scroll, max_scroll)

        for screen_row in range(content_h):
            idx = self.scroll + screen_row
            if idx >= len(rows):
                break
            self._draw_row(stdscr, content_y + screen_row, w, rows[idx], idx == self.cursor)

        if self.help_visible:
            self._draw_help(stdscr, h, w)

        stdscr.refresh()

    def _draw_status_bar(self, stdscr: "curses.window", w: int) -> None:
        attr = _cp(_CP_STATUS) | curses.A_BOLD
        line1 = (
            "VIEW COMMIT: (j/k/↓/↑) move; (l/h/→/←) open/close; "
            "(f)old (F)old-all; (J/K) jump same-kind"
        )
        line2 = "             (g/G) top/bot; (?) help; (q)uit"
        # Pad to full width so the colored bar covers the whole line.
        stdscr.addnstr(0, 0, line1.ljust(w), w - 1, attr)
        stdscr.addnstr(1, 0, line2.ljust(w), w - 1, attr)

    def _draw_row(
        self,
        stdscr: "curses.window",
        y: int,
        w: int,
        row: Row,
        selected: bool,
    ) -> None:
        # Each row is rendered as one or more (text, color-attr)
        # segments so we can put green ``+`` chars and red ``-`` chars
        # on the same line as the yellow file header — same idiom as
        # ``git diff --stat`` in a color terminal. Most row kinds
        # return a single segment.
        segments = self._format_row(row)
        base = curses.A_REVERSE if selected else 0
        x = 0
        for text, attr in segments:
            if x >= w - 1:
                break
            avail = w - 1 - x
            clipped = text[:avail]
            try:
                stdscr.addnstr(y, x, clipped, avail, attr | base)
            except curses.error:
                # Curses raises on the bottom-right cell; nothing we
                # can do about it and it's harmless.
                pass
            x += len(clipped)
        # Pad the rest of the line with the base attribute so the
        # reverse-video cursor highlight extends across the whole row,
        # not just the text — same trick crecord uses.
        if x < w - 1 and base:
            try:
                stdscr.addnstr(y, x, " " * (w - 1 - x), w - 1 - x, base)
            except curses.error:
                pass

    def _stat_bar_widths(
        self, f: File, max_total: int, width: int = _STAT_BAR_WIDTH
    ) -> tuple[int, int]:
        """Compute ``(n_plus, n_minus)`` for ``f``'s stat bar.

        Mirrors ``git diff --stat`` scaling: when the most-changed file
        in the commit has fewer than ``width`` total changes, every
        file gets one character per change (1:1 mapping). Otherwise
        all files are scaled so the most-changed file fills ``width``,
        and any non-zero side gets at least one character so a file
        with a single deletion doesn't visually round to "nothing
        happened."
        """

        total = f.additions + f.deletions
        if total == 0 or max_total == 0:
            return 0, 0
        if max_total <= width:
            return f.additions, f.deletions
        scale = width / max_total
        adds = max(1 if f.additions else 0, round(f.additions * scale))
        dels = max(1 if f.deletions else 0, round(f.deletions * scale))
        return adds, dels

    def _format_row(self, row: Row) -> list[tuple[str, int]]:
        indent = _INDENT * row.depth

        if row.kind == "commit":
            c: Commit = row.item
            glyph = _GLYPH_CLOSED if c.folded else _GLYPH_OPEN
            if c.is_working_tree:
                # The synthetic "uncommitted changes" entry has no
                # sha, author, or date — render it in cyan with a
                # ``working tree`` placeholder so it reads as a
                # different *kind* of thing than the magenta commit
                # rows below it, but uses the same fold/expand affordances.
                n_files = len(c.files)
                files_word = "file" if n_files == 1 else "files"
                text = (
                    f"{glyph} working tree  {c.subject}"
                    f"  ({n_files} {files_word})"
                )
                return [(text, _cp(_CP_HUNK) | curses.A_BOLD)]
            # ``--date=iso`` gives us ``YYYY-MM-DD HH:MM:SS ±HHMM``; for
            # the log row the calendar date alone is enough, and it
            # leaves room for a longer subject line on narrow terminals.
            date_short = c.date[:10]
            if c._loaded:
                n_files = len(c.files)
                files_word = "file" if n_files == 1 else "files"
                suffix = f"  ({n_files} {files_word})"
            else:
                # No file count until the diff has actually been
                # fetched — showing ``(0 files)`` for every unfetched
                # commit in the log would be both wrong and noisy.
                suffix = ""
            text = (
                f"{glyph} {c.short_sha}  {date_short}  {c.subject}  "
                f"— {c.author}{suffix}"
            )
            return [(text, _cp(_CP_COMMIT) | curses.A_BOLD)]

        if row.kind == "message":
            msg: Message = row.item
            glyph = _GLYPH_CLOSED if msg.folded else _GLYPH_OPEN
            n = len(msg.lines)
            word = "line" if n == 1 else "lines"
            text = f"{indent}{glyph} (commit message — {n} {word})"
            return [(text, _cp(_CP_DIM) | curses.A_DIM)]

        if row.kind == "message_line":
            # The two-space pad keeps the prose left-aligned with file
            # paths above and hunk bodies below, so the eye picks up a
            # consistent left margin regardless of which subtree it's
            # scanning.
            text = f"{indent}  {row.item}"
            return [(text, _cp(_CP_DIM))]

        if row.kind == "file":
            f: File = row.item
            glyph = _GLYPH_CLOSED if f.folded else _GLYPH_OPEN
            path = f.path
            if f.old_path and f.old_path != f.path:
                path = f"{f.old_path} → {f.path}"
            # Pad the prefix to the widest one in the visible set so
            # the counts (and therefore the stat bar) start at a
            # common column across all file rows. This is the same
            # alignment trick ``git diff --stat`` does to its paths.
            prefix = f"{indent}{glyph} [{f.status}] {path}"
            prefix_padded = prefix.ljust(self._file_row_max_prefix)
            if f.binary:
                # Binary files keep the aligned prefix but show only a
                # ``(binary)`` label where the counts would otherwise
                # be — no bar, since there's nothing to draw.
                return [
                    (prefix_padded + "  ", _cp(_CP_FILE)),
                    ("(binary)", _cp(_CP_FILE)),
                ]
            # ``+N -M`` instead of ``(+N -M)`` — closer to GitHub's
            # PR-summary and ``git --shortstat`` idiom — with each
            # count carrying the same color as the matching sigil on
            # diff body lines (green for ``+``, red for ``-``).
            add_str = f"+{f.additions}"
            del_str = f"-{f.deletions}"
            counts_len = len(add_str) + 1 + len(del_str)
            # After the (variable-width) counts, pad to the widest
            # observed counts and then add a two-space gap before the
            # bar. The padding lives in the file color so the
            # reverse-video cursor highlight still extends cleanly.
            gap_width = (self._file_row_max_counts - counts_len) + 2
            segments: list[tuple[str, int]] = [
                (prefix_padded + "  ", _cp(_CP_FILE)),
                (add_str, _cp(_CP_ADD)),
                (" ", _cp(_CP_FILE)),
                (del_str, _cp(_CP_DEL)),
                (" " * gap_width, _cp(_CP_FILE)),
            ]
            # Scale bars against the largest file *anywhere in the
            # currently-visible set*, not just inside this commit, so
            # you can compare a hunk-heavy file in one commit to a
            # quiet file in another. The trade-off is that opening or
            # closing a commit can shift the scaling for everyone
            # else — visible_rows recomputes the max on each rebuild
            # so the bars stay in sync.
            n_add, n_del = self._stat_bar_widths(f, self._file_row_max_changes)
            if n_add:
                segments.append(("+" * n_add, _cp(_CP_ADD)))
            if n_del:
                segments.append(("-" * n_del, _cp(_CP_DEL)))
            return segments

        if row.kind == "hunk":
            hk: Hunk = row.item
            glyph = _GLYPH_CLOSED if hk.folded else _GLYPH_OPEN
            text = f"{indent}{glyph} {hk.header}"
            return [(text, _cp(_CP_HUNK))]

        # line
        ln: Line = row.item
        if ln.kind == "+":
            color = _cp(_CP_ADD)
        elif ln.kind == "-":
            color = _cp(_CP_DEL)
        else:
            color = _cp(_CP_DIM)
        # Two-space indent under the hunk header keeps the marker column
        # aligned with the hunk's glyph, which makes scanning easy.
        text = f"{indent}  {ln.kind}{ln.text}"
        return [(text, color)]

    def _draw_help(self, stdscr: "curses.window", h: int, w: int) -> None:
        """Overlay a help panel centered on screen."""

        lines = [
            "  gitpeek — read-only commit browser",
            "",
            "  Navigation",
            "    j / ↓           next row",
            "    k / ↑           previous row",
            "    l / →           open / step into child",
            "    h / ←           close / step out to parent",
            "    J / PgDn        next item of same kind",
            "    K / PgUp        previous item of same kind",
            "    Ctrl-D          half-page down",
            "    Ctrl-U          half-page up",
            "    g / G           top / bottom",
            "",
            "  Folding",
            "    f / SPACE       fold / unfold current item",
            "    F               fold current + all ancestors",
            "    *               toggle subtree (open all if any closed)",
            "    zM              fold; on a commit row affects every",
            "                    commit, otherwise just the current one",
            "    zR              unfold; same scope rules as zM",
            "",
            "  Other",
            "    ?               toggle this help",
            "    q / Q           quit",
            "",
            "  Press any key to dismiss.",
        ]
        box_h = len(lines) + 2
        box_w = max(len(line) for line in lines) + 4
        if box_h > h or box_w > w:
            # Don't try to draw a help panel that won't fit.
            return
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        attr = _cp(_CP_STATUS)
        # Box outline + filled body. We draw the corners and edges by
        # hand instead of using ``stdscr.border`` because that draws on
        # the whole screen, not on a subwindow.
        top = "┌" + "─" * (box_w - 2) + "┐"
        bot = "└" + "─" * (box_w - 2) + "┘"
        stdscr.addnstr(y0, x0, top, box_w, attr)
        stdscr.addnstr(y0 + box_h - 1, x0, bot, box_w, attr)
        for i in range(1, box_h - 1):
            stdscr.addnstr(y0 + i, x0, "│", 1, attr)
            stdscr.addnstr(y0 + i, x0 + box_w - 1, "│", 1, attr)
        for i, line in enumerate(lines):
            stdscr.addnstr(y0 + 1 + i, x0 + 1, line.ljust(box_w - 2), box_w - 2, attr)


def run(commits: list[Commit], cwd: str | None = None) -> None:
    """Convenience wrapper: set locale, hand off to :func:`curses.wrapper`."""

    # Without this, curses on macOS sometimes fails to render the
    # triangle glyphs in our row markers.
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(UI(commits, cwd=cwd).run)
