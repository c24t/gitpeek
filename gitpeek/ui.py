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

from gitpeek.diff import Commit, File, Hunk, Line


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
    """Stateful curses application — instantiate once, call :meth:`run`."""

    def __init__(self, commit: Commit) -> None:
        self.commit = commit
        # By default we want the commit row open (so the user sees
        # files immediately) but every file *and* every hunk collapsed,
        # so each press of ``l`` reveals exactly one more level. That
        # matches the depth-by-depth feel of ``hg commit -i`` and keeps
        # the initial view to a screen of file headers even for big
        # commits.
        for f in commit.files:
            f.folded = True
            for h in f.hunks:
                h.folded = True
        self.cursor = 0
        self.scroll = 0
        self.help_visible = False
        self._cached_rows: list[Row] | None = None

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
        rows: list[Row] = [Row(self.commit, 0, "commit")]
        if not self.commit.folded:
            for f in self.commit.files:
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
            return bool(item.files)
        if isinstance(item, File):
            return bool(item.hunks)
        if isinstance(item, Hunk):
            return bool(item.lines)
        return False

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

        rows = self.visible_rows()
        n = len(rows)
        row = rows[self.cursor]

        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("?"):
            self.help_visible = True
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
            n_files = len(c.files)
            files_word = "file" if n_files == 1 else "files"
            text = (
                f"{glyph} commit {c.short_sha}  {c.subject}  "
                f"— {c.author}, {c.date}  ({n_files} {files_word})"
            )
            return text, curses.color_pair(_CP_COMMIT) | curses.A_BOLD

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


def run(commit: Commit) -> None:
    """Convenience wrapper: set locale, hand off to :func:`curses.wrapper`."""

    # Without this, curses on macOS sometimes fails to render the
    # triangle glyphs in our row markers.
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(UI(commit).run)
