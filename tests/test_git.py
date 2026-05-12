"""Integration tests for :mod:`gitpeek.git`.

These spin up a real, throwaway git repository per test (via the
pytest ``tmp_path`` fixture) and drive the loader functions against
it. Running git for real is heavier than a mock, but it's also the
only honest way to verify that our argument formatting, parsing, and
working-tree synthesis line up with whatever the local git binary
actually emits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitpeek.git import GitError, load_log, load_uncommitted


def _git(cwd: Path, *args: str) -> None:
    """Run a quiet ``git`` command in ``cwd``; raise on failure."""

    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _bootstrap_repo(tmp_path: Path) -> Path:
    """Create an empty repo with one initial commit and return its path."""

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "file.txt").write_text("hello\n")
    _git(tmp_path, "add", "file.txt")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


# -- load_log ----------------------------------------------------------


def test_load_log_empty_ref_raises(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    # An empty repo has no HEAD — ``git log HEAD`` should error and
    # we should surface that as a clean GitError, not a traceback.
    with pytest.raises(GitError):
        load_log(cwd=str(tmp_path))


def test_load_log_returns_commits_in_reverse_order(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    (repo / "file.txt").write_text("hello\nworld\n")
    _git(repo, "commit", "-aq", "-m", "second")
    (repo / "file.txt").write_text("hello\nworld\n!\n")
    _git(repo, "commit", "-aq", "-m", "third")

    commits = load_log(cwd=str(repo))
    assert [c.subject for c in commits] == ["third", "second", "initial"]
    # ``_loaded`` defaults to False — log fetch is metadata only.
    assert all(c._loaded is False for c in commits)


# -- load_uncommitted --------------------------------------------------


def test_load_uncommitted_returns_none_on_clean_tree(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    assert load_uncommitted(cwd=str(repo)) is None


def test_load_uncommitted_picks_up_modified_files(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    (repo / "file.txt").write_text("hello\nchanged\n")

    wt = load_uncommitted(cwd=str(repo))
    assert wt is not None
    assert wt.is_working_tree is True
    assert wt._loaded is True
    assert wt.subject == "Uncommitted changes"
    paths = [f.path for f in wt.files]
    assert "file.txt" in paths


def test_load_uncommitted_picks_up_staged_changes(tmp_path: Path) -> None:
    """``git diff HEAD`` shows staged content, so we should too."""
    repo = _bootstrap_repo(tmp_path)
    (repo / "file.txt").write_text("hello\nstaged\n")
    _git(repo, "add", "file.txt")

    wt = load_uncommitted(cwd=str(repo))
    assert wt is not None
    paths = [f.path for f in wt.files]
    assert "file.txt" in paths


def test_load_uncommitted_synthesises_untracked_as_additions(tmp_path: Path) -> None:
    """A brand-new file should appear in the section as an ``A`` entry,

    with all of its lines as ``+`` body lines — same shape git uses
    for a real new-file diff."""
    repo = _bootstrap_repo(tmp_path)
    (repo / "new.txt").write_text("first line\nsecond line\n")

    wt = load_uncommitted(cwd=str(repo))
    assert wt is not None
    new = next(f for f in wt.files if f.path == "new.txt")
    assert new.status == "A"
    assert new.additions == 2
    assert new.deletions == 0
    assert [ln.kind for ln in new.hunks[0].lines] == ["+", "+"]
    assert [ln.text for ln in new.hunks[0].lines] == ["first line", "second line"]


def test_load_uncommitted_marks_undecodable_untracked_as_binary(
    tmp_path: Path,
) -> None:
    """Binary content can't be UTF-8 decoded; surface as ``binary`` so

    we don't crash trying to splitlines on raw bytes."""
    repo = _bootstrap_repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\xff\x00\xfe\x01")

    wt = load_uncommitted(cwd=str(repo))
    assert wt is not None
    bin_file = next(f for f in wt.files if f.path == "blob.bin")
    assert bin_file.binary is True
    assert bin_file.hunks == []


def test_load_uncommitted_ignores_gitignored_files(tmp_path: Path) -> None:
    """Files matching ``.gitignore`` shouldn't appear — same rule as

    ``git ls-files --others --exclude-standard``."""
    repo = _bootstrap_repo(tmp_path)
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("hidden\n")

    wt = load_uncommitted(cwd=str(repo))
    # The .gitignore itself is untracked and should appear; secret.txt
    # must not.
    assert wt is not None
    paths = [f.path for f in wt.files]
    assert ".gitignore" in paths
    assert "secret.txt" not in paths
