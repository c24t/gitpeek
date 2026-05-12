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


@dataclass
class Row:
    """One renderable line in the flattened tree view.

    ``item`` is the underlying ``Commit`` / ``File`` / ``Hunk`` / ``Line``
    node; ``depth`` is its nesting level (used both for indentation and
    for "jump to parent"); ``kind`` is a short string used by the
    "next/prev of same kind" navigation.
    """

    item: Any
    depth: int
    kind: str  # 'commit' | 'file' | 'hunk' | 'line'


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
                rows.append(Row(msg, 1, "message"))
                if not msg.folded:
                    for line in msg.lines:
                        rows.append(Row(line, 2, "message_line"))
            for f in commit.files:
                rows.append(Row(f, 1, "file"))
                if not f.folded:
                    for h in f.hunks:
                        rows.append(Row(h, 2, "hunk"))
                        if not h.folded:
                            for ln in h.lines:
                                rows.append(Row(ln, 3, "line"))
        self._cached_rows = rows
        return rows

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

    def _fold_tree(self, folded: bool) -> None:
        """Set fold state on every File and Hunk in the commit.

        Used by ``zM`` / ``zR``. We deliberately *don't* fold the commit
        row itself — that's the one row whose presence is load-bearing
        for orientation (it tells you which commit you're looking at).
        Folding it would just give you a one-row screen of "press l to
        get started", which is hostile rather than helpful.
        """

        for commit in self.commits:
            if not folded:
                # ``zR`` means "open everything." For commits we
                # haven't seen yet, that requires fetching the diff so
                # the unfold has something to reveal. Loading every
                # commit is the user's explicit ask — they pressed the
                # "give me all the things" key — and they can ``zM``
                # back if it turns out to be too much.
                self._ensure_loaded(commit)
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
            if ch == ord("M"):
                self._fold_tree(True)
            elif ch == ord("R"):
                self._fold_tree(False)
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
            # Plain toggle. ``l``/``h`` are directional; ``f`` is the
            # "I just want to flip this regardless of where the cursor
            # ends up" key.
            item = row.item
            # Same lazy-load rationale as ``l``: a folded commit needs
            # its diff fetched before we can usefully expand it.
            if isinstance(item, Commit) and item.folded:
                self._ensure_loaded(item)
            if hasattr(item, "folded") and self._has_children(row):
                item.folded = not item.folded
                self._invalidate()

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
            # crecord uses space to (un-)select; we have no selection,
            # but we eat the keypress so the muscle memory doesn't move
            # the cursor unexpectedly.
            pass

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
        attr = curses.color_pair(_CP_STATUS) | curses.A_BOLD
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
        text, attr = self._format_row(row)
        if selected:
            attr |= curses.A_REVERSE
        # Pad so the reverse-video cursor highlight extends across the
        # whole line, not just the text — same trick crecord uses.
        padded = text.ljust(w)
        try:
            stdscr.addnstr(y, 0, padded, w - 1, attr)
        except curses.error:
            # Curses raises on the bottom-right cell; nothing we can do
            # about it and it's harmless.
            pass

    def _format_row(self, row: Row) -> tuple[str, int]:
        indent = _INDENT * row.depth

        if row.kind == "commit":
            c: Commit = row.item
            glyph = _GLYPH_CLOSED if c.folded else _GLYPH_OPEN
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
            return text, curses.color_pair(_CP_COMMIT) | curses.A_BOLD

        if row.kind == "message":
            msg: Message = row.item
            glyph = _GLYPH_CLOSED if msg.folded else _GLYPH_OPEN
            n = len(msg.lines)
            word = "line" if n == 1 else "lines"
            text = f"{indent}{glyph} (commit message — {n} {word})"
            return text, curses.color_pair(_CP_DIM) | curses.A_DIM

        if row.kind == "message_line":
            # The two-space pad keeps the prose left-aligned with file
            # paths above and hunk bodies below, so the eye picks up a
            # consistent left margin regardless of which subtree it's
            # scanning.
            text = f"{indent}  {row.item}"
            return text, curses.color_pair(_CP_DIM)

        if row.kind == "file":
            f: File = row.item
            glyph = _GLYPH_CLOSED if f.folded else _GLYPH_OPEN
            path = f.path
            if f.old_path and f.old_path != f.path:
                path = f"{f.old_path} → {f.path}"
            if f.binary:
                stats = "(binary)"
            else:
                stats = f"(+{f.additions} -{f.deletions})"
            text = f"{indent}{glyph} [{f.status}] {path}  {stats}"
            return text, curses.color_pair(_CP_FILE)

        if row.kind == "hunk":
            hk: Hunk = row.item
            glyph = _GLYPH_CLOSED if hk.folded else _GLYPH_OPEN
            text = f"{indent}{glyph} {hk.header}"
            return text, curses.color_pair(_CP_HUNK)

        # line
        ln: Line = row.item
        if ln.kind == "+":
            color = curses.color_pair(_CP_ADD)
        elif ln.kind == "-":
            color = curses.color_pair(_CP_DEL)
        else:
            color = curses.color_pair(_CP_DIM)
        # Two-space indent under the hunk header keeps the marker column
        # aligned with the hunk's glyph, which makes scanning easy.
        text = f"{indent}  {ln.kind}{ln.text}"
        return text, color

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
            "    g / G           top / bottom",
            "",
            "  Folding",
            "    f               fold / unfold current item",
            "    F               fold current + all ancestors",
            "    *               toggle subtree (open all if any closed)",
            "    zM              fold every file and hunk",
            "    zR              unfold every file and hunk",
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
        attr = curses.color_pair(_CP_STATUS)
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
