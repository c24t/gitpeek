# gitpeek

A read-only terminal browser for git commits, modeled after Mercurial's `hg
commit -i` (the `crecord` interactive selector). gitpeek walks the git log as a
foldable tree, shows `git diff --stat`-style bars next to each file, and hands
off to vim with a [vim-fugitive][fugitive] diff when you press `Enter`.

```
▼ working tree  Uncommitted changes  (3 files)
  ▼ [M] src/foo.py     +12 -5    ++++-
    ▼ @@ -42,7 +42,8 @@
       def render(self):
      -    return template.render(self.data)
      +    cleaned = self._sanitise(self.data)
      +    return template.render(cleaned)
  ▶ [M] tests/test.py  +30 -2    +++++++++++-
  ▶ [A] notes.md       +14 -0   +++++
▶ 7abbb4c  2026-05-11  Implement /play as a synchronous sequence  — Author
▶ 2a9788c  2026-05-11  Add albums service and add-album CLI         — Author
```

[fugitive]: https://github.com/tpope/vim-fugitive

## Why this exists

`hg`'s `crecord` selector is one of the more pleasant terminal UIs in version
control: a foldable tree where you walk commit → file → hunk → line with `l` /
`h`, jump siblings with `J` / `K`, and collapse subtrees with `f` / `F`. `git
log -p` is great for grepping but useless for browsing structure. `tig` is
close but doesn't quite match the idiom.  gitpeek fills that gap — read-only,
no selection, just navigation, with the same keybindings.

## Install

stdlib-only Python 3.10+:

```sh
pipx install git+https://github.com/cnk/gitpeek
```

Optional: [vim-fugitive][fugitive] for the `Enter`-handler diff view.

## Usage

```sh
gitpeek                  # all commits in the current repo
gitpeek -n 50            # cap the log at 50 commits
gitpeek HEAD~10..HEAD    # only the last 10 commits
gitpeek -C /path/to/repo # like `git -C`
```

If the working tree has uncommitted changes — modified, staged, or untracked —
they appear as a synthetic "working tree" section above the first commit.

## Keybindings

Navigation keys match `crecord` where they overlap; selection-related keys
(`SPACE`, `A`) are read-only no-ops or rebound to fold-toggle.

### Navigation

| Key                | Action                                          |
| ------------------ | ----------------------------------------------- |
| `j` / `↓`          | next row                                        |
| `k` / `↑`          | previous row                                    |
| `l` / `→`          | open / step into child                          |
| `h` / `←`          | close / step out to parent                      |
| `J` / `PgDn`       | next item of the same kind (e.g. next file)     |
| `K` / `PgUp`       | previous item of the same kind                  |
| `Ctrl-D` / `Ctrl-U` | half-page down / up (vim-style)                |
| `g` / `G`          | top / bottom                                    |

### Folding

| Key            | Action                                                              |
| -------------- | ------------------------------------------------------------------- |
| `f` / `SPACE`  | fold / unfold current item                                          |
| `F`            | fold current + all ancestors (escape back toward the log)           |
| `*`            | toggle subtree — opens all if any closed; only closes when fully open |
| `zM`           | fold; on a commit row affects every commit, otherwise just the current one |
| `zR`           | unfold; same scope rules as `zM`                                    |

### Other

| Key          | Action                                                                |
| ------------ | --------------------------------------------------------------------- |
| `Enter`      | open the file in vim with `:Gvdiffsplit` against the previous revision |
| `?`          | toggle help overlay                                                    |
| `q` / `Q`    | quit                                                                  |

## The tree

```
working tree (if any)
  ├── file
  │   └── hunk
  │       └── line
  └── …
commit
  ├── message     (the commit body, foldable)
  │   └── body line
  ├── file
  │   └── hunk
  │       └── line
  └── …
commit
  └── …
```

Each node folds independently. Commits start folded inside the log so the
initial screen is a clean log of subject lines; files and hunks inside an
opened commit also start folded, so each press of `l` reveals exactly one more
level.

## Stat bars

Each file row carries a `git diff --stat`-style bar with green `+` and red `-`
characters next to the count. The count itself is colored to match — `+12`
green, `-5` red — same as the `+` / `-` sigils inside diff hunks.

The bar **scales across all currently-open file rows in the log**, not just
within a single commit. The same bar length corresponds to the same number of
changes whether the file is in the working tree or twenty commits back. The
left edge of every bar is padded to a common column so they line up visually
for side-by-side comparison.

Trade-off: a single very large file in any open commit dominates the scale;
smaller files alongside it shrink toward a 1-character minimum.  Close commits
you don't care about (with `h` or `zM`) to bring small bars back into
proportion.

## Markdown hunk-header context

By default, git's hunk-header "function context" heuristic picks the nearest
preceding non-blank line, which for Markdown ends up being arbitrary prose
instead of the section heading. To get headings:

```ini
# ~/.gitconfig
[diff "markdown"]
    xfuncname = "^#+ .*$"
```

```
# ~/.config/git/attributes
*.md       diff=markdown
*.mdx      diff=markdown
*.markdown diff=markdown
```

This affects every git tool that emits unified diffs — `git diff`, `git log
-p`, GitHub-rendered patches, and of course gitpeek's hunk headers.

## Vim integration (`Enter`)

Pressing `Enter` on a file / hunk / line row:

1. Suspends the gitpeek screen.
2. Launches `vim` with the file at the commit (`:Gedit COMMIT:path`).
3. Opens a vertical fugitive split against the parent
   (`:Gvdiffsplit COMMIT^`).
4. Drops the cursor on the post-image line you were looking at.

When vim exits, gitpeek redraws where you left it.

Special cases:

- **Renames**: the pre-image side references the old path (`:Gvdiffsplit
  COMMIT^:old/path`) so fugitive can find the file.
- **Deletions**: no diff split — there's nothing to diff against. Just `:Gedit
  COMMIT^:path` to open the pre-deletion version.
- **Working tree files**: `:edit path` + `:Gvdiffsplit HEAD`.
- **Binary files**, commit headers, message rows: no-op.

If `vim` isn't on `PATH`, the keypress silently no-ops — gitpeek's screen
redraws and you stay where you were rather than a traceback landing in the
terminal.

## Caveats

- **Read-only by design**. There's no `commit -i`-style selection; the
  selection keys (`A`, `e`, `c`, `r`) are absent.
- **Working-tree section refresh** only happens at startup. If files change
  while gitpeek is running, quit and reopen to see them.
- **`zR` on big logs** triggers a `git show` for every commit in scope. On
  thousand-commit logs that's a few seconds of pause.
- **Untracked file bodies** are read into memory via Python `open()`.  A huge
  untracked log file will be loaded eagerly.
- **Paths with spaces** in the vim launch are not currently shell-escaped. Code
  paths don't usually contain spaces, so this rarely bites in practice.

## Development

```sh
git clone https://github.com/c24t/gitpeek
cd gitpeek
python -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest
```

Tests in `tests/` cover the unified-diff parser, the navigation model, the
stat-bar scaling, the vim-arg construction, and (in `test_git.py`) a real-`git`
integration suite that spins up a throwaway repo per test.

## License

[MIT](LICENSE).
