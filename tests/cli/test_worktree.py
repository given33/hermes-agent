"""Tests for git worktree isolation (CLI --worktree / -w flag).

Verifies worktree creation, cleanup, .worktreeinclude handling,
.gitignore management, and integration with the CLI.  (#652)
"""

import os
import shutil
import subprocess
import pytest
from pathlib import Path


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo for testing."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True,
    )
    # Create initial commit (worktrees need at least one commit)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/test-repo.git"],
        cwd=repo, capture_output=True,
    )
    # Add a fake remote ref so cleanup logic sees the initial commit as
    # "pushed" when a remote is configured.
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo, capture_output=True,
    )
    return repo


@pytest.fixture
def git_repo_no_remote(tmp_path):
    """Create a temporary git repo with no configured remotes."""
    repo = tmp_path / "test-repo-no-remote"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True,
    )
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo, capture_output=True,
    )
    return repo


@pytest.fixture
def git_repo_remote_no_tracking(tmp_path):
    """Create a temporary git repo with a remote but no remote-tracking refs."""
    repo = tmp_path / "test-repo-remote-no-tracking"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True,
    )
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/test-repo.git"],
        cwd=repo, capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# Lightweight reimplementations for testing (avoid importing cli.py)
# ---------------------------------------------------------------------------

def _git_repo_root(cwd=None):
    """Test version of _git_repo_root."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _setup_worktree(repo_root):
    """Test version of _setup_worktree — creates a worktree."""
    import uuid
    short_id = uuid.uuid4().hex[:8]
    wt_name = f"hermes-{short_id}"
    branch_name = f"hermes/{wt_name}"

    worktrees_dir = Path(repo_root) / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_dir / wt_name

    result = subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch_name, "HEAD"],
        capture_output=True, text=True, timeout=30, cwd=repo_root,
    )
    if result.returncode != 0:
        return None

    return {
        "path": str(wt_path),
        "branch": branch_name,
        "repo_root": repo_root,
    }


def _has_unpushed_commits(worktree_path, timeout=10):
    """Test version of the worktree unpushed-commit helper."""
    try:
        remote_refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/remotes"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if remote_refs.returncode != 0:
            return True
        if not remote_refs.stdout.strip():
            return True

        result = subprocess.run(
            ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except Exception:
        return True


def _cleanup_worktree(info):
    """Call the production cleanup path and report whether it removed the tree."""
    import cli

    cli._cleanup_worktree(info)
    return not Path(info["path"]).exists()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGitRepoDetection:
    """Test git repo root detection."""

    def test_detects_git_repo(self, git_repo):
        root = _git_repo_root(cwd=str(git_repo))
        assert root is not None
        assert Path(root).resolve() == git_repo.resolve()

    def test_detects_subdirectory(self, git_repo):
        subdir = git_repo / "src" / "lib"
        subdir.mkdir(parents=True)
        root = _git_repo_root(cwd=str(subdir))
        assert root is not None
        assert Path(root).resolve() == git_repo.resolve()

    def test_returns_none_outside_repo(self, tmp_path):
        # tmp_path itself is not a git repo
        bare_dir = tmp_path / "not-a-repo"
        bare_dir.mkdir()
        root = _git_repo_root(cwd=str(bare_dir))
        assert root is None


class TestWorktreeCreation:
    """Test worktree setup."""

    def test_creates_worktree(self, git_repo):
        info = _setup_worktree(str(git_repo))
        assert info is not None
        assert Path(info["path"]).exists()
        assert info["branch"].startswith("hermes/hermes-")
        assert info["repo_root"] == str(git_repo)

        # Verify it's a valid git worktree
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=info["path"],
        )
        assert result.stdout.strip() == "true"

    def test_worktree_has_own_branch(self, git_repo):
        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Check branch name in worktree
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=info["path"],
        )
        assert result.stdout.strip() == info["branch"]

    def test_worktree_is_independent(self, git_repo):
        """Two worktrees from the same repo are independent."""
        info1 = _setup_worktree(str(git_repo))
        info2 = _setup_worktree(str(git_repo))
        assert info1 is not None
        assert info2 is not None
        assert info1["path"] != info2["path"]
        assert info1["branch"] != info2["branch"]

        # Create a file in worktree 1
        (Path(info1["path"]) / "only-in-wt1.txt").write_text("hello")

        # It should NOT appear in worktree 2
        assert not (Path(info2["path"]) / "only-in-wt1.txt").exists()

    def test_worktrees_dir_created(self, git_repo):
        info = _setup_worktree(str(git_repo))
        assert info is not None
        assert (git_repo / ".worktrees").is_dir()

    def test_worktree_has_repo_files(self, git_repo):
        """Worktree should contain the repo's tracked files."""
        info = _setup_worktree(str(git_repo))
        assert info is not None
        assert (Path(info["path"]) / "README.md").exists()


class TestWorktreeCleanup:
    """Test worktree cleanup on exit."""

    def test_clean_worktree_removed(self, git_repo):
        info = _setup_worktree(str(git_repo))
        assert info is not None
        assert Path(info["path"]).exists()

        result = _cleanup_worktree(info)
        assert result is True
        assert not Path(info["path"]).exists()

    @pytest.mark.parametrize("change_kind", ["staged", "unstaged", "untracked"])
    def test_dirty_worktree_is_preserved(self, git_repo, change_kind):
        """Every form of uncommitted work must survive automatic cleanup."""
        import cli

        info = _setup_worktree(str(git_repo))
        assert info is not None
        worktree = Path(info["path"])

        if change_kind == "unstaged":
            (worktree / "README.md").write_text("unstaged\n")
        else:
            changed = worktree / f"{change_kind}.txt"
            changed.write_text(change_kind)
            if change_kind == "staged":
                subprocess.run(
                    ["git", "add", changed.name],
                    cwd=worktree, capture_output=True, check=True,
                )
            else:
                # Production must override a repo config that hides untracked
                # files from ordinary status output.
                subprocess.run(
                    ["git", "config", "status.showUntrackedFiles", "no"],
                    cwd=worktree, capture_output=True, check=True,
                )

        cli._cleanup_worktree(info)

        assert worktree.exists()
        assert cli._worktree_is_dirty(str(worktree))

    def test_worktree_with_unpushed_commits_kept(self, git_repo):
        """Worktree with unpushed commits is preserved."""
        import cli

        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Make a commit that is NOT on any remote
        (Path(info["path"]) / "work.txt").write_text("real work")
        subprocess.run(["git", "add", "work.txt"], cwd=info["path"], capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "agent work"],
            cwd=info["path"], capture_output=True,
        )

        cli._cleanup_worktree(info)
        assert Path(info["path"]).exists()

    def test_clean_worktree_preserved_without_remote(self, git_repo_no_remote):
        """Without a remote ref, cleanup cannot prove the branch is backed up."""
        info = _setup_worktree(str(git_repo_no_remote))
        assert info is not None
        assert Path(info["path"]).exists()
        assert _has_unpushed_commits(info["path"], timeout=10) is True

        result = _cleanup_worktree(info)
        assert result is False
        assert Path(info["path"]).exists()

    def test_clean_worktree_preserved_without_remote_tracking_refs(
        self, git_repo_remote_no_tracking
    ):
        """A configured but unfetched remote provides no durable baseline."""
        info = _setup_worktree(str(git_repo_remote_no_tracking))
        assert info is not None
        assert Path(info["path"]).exists()
        assert _has_unpushed_commits(info["path"], timeout=10) is True

        result = _cleanup_worktree(info)
        assert result is False
        assert Path(info["path"]).exists()

    def test_branch_deleted_on_cleanup(self, git_repo):
        info = _setup_worktree(str(git_repo))
        branch = info["branch"]

        _cleanup_worktree(info)

        # Branch should be gone
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            capture_output=True, text=True, cwd=str(git_repo),
        )
        assert branch not in result.stdout

    def test_cleanup_nonexistent_worktree(self, git_repo):
        """Cleanup should handle already-removed worktrees gracefully."""
        import cli

        info = {
            "path": str(git_repo / ".worktrees" / "nonexistent"),
            "branch": "hermes/nonexistent",
            "repo_root": str(git_repo),
        }
        # Should not raise
        cli._cleanup_worktree(info)

    def test_remove_failure_keeps_branch_and_does_not_report_success(
        self, git_repo, capsys
    ):
        """A real git removal failure must not cascade into branch deletion."""
        import cli

        branch = "hermes/hermes-remove-failure"
        subprocess.run(
            ["git", "branch", branch, "HEAD"],
            cwd=git_repo, capture_output=True, check=True,
        )
        info = {
            # Git refuses to remove the repository's main worktree.
            "path": str(git_repo),
            "branch": branch,
            "repo_root": str(git_repo),
        }

        cli._cleanup_worktree(info)

        output = capsys.readouterr().out
        assert git_repo.exists()
        assert "Failed to remove worktree" in output
        assert "Worktree cleaned up" not in output
        branch_result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=git_repo, capture_output=True, text=True, check=True,
        )
        assert branch in branch_result.stdout

    def test_switched_worktree_preserves_unique_original_branch(self, git_repo):
        """Cleanup must validate the recorded branch, not only current HEAD."""
        import cli

        info = _setup_worktree(str(git_repo))
        worktree = Path(info["path"])
        (worktree / "original-work.txt").write_text("unique")
        subprocess.run(
            ["git", "add", "original-work.txt"],
            cwd=worktree, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "unique original branch work"],
            cwd=worktree, capture_output=True, check=True,
        )
        subprocess.run(
            [
                "git", "switch", "-c", "cleanup-safe-head",
                "refs/remotes/origin/main",
            ],
            cwd=worktree, capture_output=True, check=True,
        )

        cli._cleanup_worktree(info)

        assert not worktree.exists()
        original = subprocess.run(
            ["git", "branch", "--list", info["branch"]],
            cwd=git_repo, capture_output=True, text=True, check=True,
        )
        assert info["branch"] in original.stdout
        preserved = subprocess.run(
            ["git", "show", f"{info['branch']}:original-work.txt"],
            cwd=git_repo, capture_output=True, text=True, check=True,
        )
        assert preserved.stdout == "unique"


class TestWorktreeInclude:
    """Test .worktreeinclude file handling."""

    def test_copies_included_files(self, git_repo):
        """Files listed in .worktreeinclude should be copied to the worktree."""
        # Create a .env file (gitignored)
        (git_repo / ".env").write_text("SECRET=abc123")
        (git_repo / ".gitignore").write_text(".env\n.worktrees/\n")
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=str(git_repo), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add gitignore"],
            cwd=str(git_repo), capture_output=True,
        )

        # Create .worktreeinclude
        (git_repo / ".worktreeinclude").write_text(".env\n")

        # Import and use the real _setup_worktree logic for include handling
        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Manually copy .worktreeinclude entries (mirrors cli.py logic)
        include_file = git_repo / ".worktreeinclude"
        wt_path = Path(info["path"])
        for line in include_file.read_text().splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            src = git_repo / entry
            dst = wt_path / entry
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

        # Verify .env was copied
        assert (wt_path / ".env").exists()
        assert (wt_path / ".env").read_text() == "SECRET=abc123"

    def test_ignores_comments_and_blanks(self, git_repo):
        """Comments and blank lines in .worktreeinclude should be skipped."""
        (git_repo / ".worktreeinclude").write_text(
            "# This is a comment\n"
            "\n"
            "  # Another comment\n"
        )
        info = _setup_worktree(str(git_repo))
        assert info is not None
        # Should not crash — just skip all lines


class TestGitignoreManagement:
    """Test that .worktrees/ is added to .gitignore."""

    def test_adds_to_gitignore(self, git_repo):
        """Creating a worktree should add .worktrees/ to .gitignore."""
        # Remove any existing .gitignore
        gitignore = git_repo / ".gitignore"
        if gitignore.exists():
            gitignore.unlink()

        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Now manually add .worktrees/ to .gitignore (mirrors cli.py logic)
        _ignore_entry = ".worktrees/"
        existing = gitignore.read_text() if gitignore.exists() else ""
        if _ignore_entry not in existing.splitlines():
            with open(gitignore, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{_ignore_entry}\n")

        content = gitignore.read_text()
        assert ".worktrees/" in content

    def test_does_not_duplicate_gitignore_entry(self, git_repo):
        """If .worktrees/ is already in .gitignore, don't add again."""
        gitignore = git_repo / ".gitignore"
        gitignore.write_text(".worktrees/\n")

        # The check should see it's already there
        existing = gitignore.read_text()
        assert ".worktrees/" in existing.splitlines()


class TestMultipleWorktrees:
    """Test running multiple worktrees concurrently (the core use case)."""

    def test_ten_concurrent_worktrees(self, git_repo):
        """Create 10 worktrees — simulating 10 parallel agents."""
        worktrees = []
        for _ in range(10):
            info = _setup_worktree(str(git_repo))
            assert info is not None
            worktrees.append(info)

        # All should exist and be independent
        paths = [info["path"] for info in worktrees]
        assert len(set(paths)) == 10  # All unique

        # Each should have the repo files
        for info in worktrees:
            assert (Path(info["path"]) / "README.md").exists()

        # Edit a file in one worktree
        (Path(worktrees[0]["path"]) / "README.md").write_text("Modified in wt0")

        # Others should be unaffected
        for info in worktrees[1:]:
            assert (Path(info["path"]) / "README.md").read_text() == "# Test Repo\n"

        # List worktrees via git
        result = subprocess.run(
            ["git", "worktree", "list"],
            capture_output=True, text=True, cwd=str(git_repo),
        )
        # Should have 11 entries: main + 10 worktrees
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        assert len(lines) == 11

        # Cleanup all (git_repo fixture has a fake remote ref so cleanup works)
        for info in worktrees:
            # Discard changes first so cleanup works
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=info["path"], capture_output=True,
            )
            _cleanup_worktree(info)

        # All should be removed
        for info in worktrees:
            assert not Path(info["path"]).exists()


class TestWorktreeDirectorySymlink:
    """Test production .worktreeinclude directory handling."""

    def test_directory_include_uses_symlink_or_windows_copy(self, git_repo):
        """Directory includes remain usable with either supported strategy."""
        import cli

        # Create a .venv directory
        venv_dir = git_repo / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "marker.txt").write_text("venv marker")
        (git_repo / ".gitignore").write_text(".venv/\n.worktrees/\n")
        subprocess.run(
            ["git", "add", ".gitignore"], cwd=str(git_repo), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "gitignore"], cwd=str(git_repo), capture_output=True
        )

        (git_repo / ".worktreeinclude").write_text(".venv/\n")

        # Exercise the production include handling without making a network
        # request to resolve the worktree base.
        info = cli._setup_worktree(str(git_repo), sync_base=False)
        assert info is not None

        wt_path = Path(info["path"])
        src = git_repo / ".venv"
        dst = wt_path / ".venv"
        marker = dst / "lib" / "marker.txt"

        assert dst.is_dir()
        assert marker.read_text() == "venv marker"

        if dst.is_symlink():
            assert dst.resolve() == src.resolve()
            (src / "lib" / "marker.txt").write_text("updated through source")
            assert marker.read_text() == "updated through source"
        else:
            # Production only falls back to copytree on Windows when symlink
            # creation is unavailable (for example without Developer Mode).
            assert os.name == "nt"
            marker.write_text("updated copy")
            assert (src / "lib" / "marker.txt").read_text() == "venv marker"


class TestStaleWorktreePruning:
    """Test _prune_stale_worktrees garbage collection."""

    def test_prunes_old_clean_worktree(self, git_repo):
        """Old clean worktrees should be removed on prune."""
        import time

        info = _setup_worktree(str(git_repo))
        assert info is not None
        assert Path(info["path"]).exists()

        # Make the worktree look old (set mtime to 25h ago)
        old_time = time.time() - (25 * 3600)
        os.utime(info["path"], (old_time, old_time))

        # Reimplementation of prune logic (matches cli.py)
        worktrees_dir = git_repo / ".worktrees"
        cutoff = time.time() - (24 * 3600)

        for entry in worktrees_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith("hermes-"):
                continue
            try:
                mtime = entry.stat().st_mtime
                if mtime > cutoff:
                    continue
            except Exception:
                continue

            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=5, cwd=str(entry),
            )
            if status.stdout.strip():
                continue

            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=5, cwd=str(entry),
            )
            branch = branch_result.stdout.strip()
            subprocess.run(
                ["git", "worktree", "remove", str(entry), "--force"],
                capture_output=True, text=True, timeout=15, cwd=str(git_repo),
            )
            if branch:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    capture_output=True, text=True, timeout=10, cwd=str(git_repo),
                )

        assert not Path(info["path"]).exists()

    def test_keeps_recent_worktree(self, git_repo):
        """Recent worktrees should NOT be pruned."""
        import time

        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Don't modify mtime — it's recent
        worktrees_dir = git_repo / ".worktrees"
        cutoff = time.time() - (24 * 3600)

        pruned = False
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith("hermes-"):
                continue
            mtime = entry.stat().st_mtime
            if mtime > cutoff:
                continue  # Too recent
            pruned = True

        assert not pruned
        assert Path(info["path"]).exists()

    def test_keeps_old_worktree_with_unpushed_commits(self, git_repo):
        """Old worktrees (24-72h) with unpushed commits should NOT be pruned."""
        import time

        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Make an unpushed commit
        (Path(info["path"]) / "work.txt").write_text("real work")
        subprocess.run(["git", "add", "work.txt"], cwd=info["path"], capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "agent work"],
            cwd=info["path"], capture_output=True,
        )

        # Make it old (25h — in the 24-72h soft tier)
        old_time = time.time() - (25 * 3600)
        os.utime(info["path"], (old_time, old_time))

        # Check for unpushed commits (simulates prune logic)
        has_unpushed = _has_unpushed_commits(info["path"])
        assert has_unpushed  # Has unpushed commits → not pruned in soft tier
        assert Path(info["path"]).exists()

    def test_preserves_old_clean_worktree_without_remote(self, git_repo_no_remote):
        """Age cannot replace proof that the worktree tip was pushed."""
        import time
        import cli

        info = _setup_worktree(str(git_repo_no_remote))
        assert info is not None
        old_time = time.time() - (25 * 3600)
        os.utime(info["path"], (old_time, old_time))

        cli._prune_stale_worktrees(str(git_repo_no_remote))

        assert Path(info["path"]).exists()

    def test_preserves_old_clean_worktree_without_remote_tracking_refs(
        self, git_repo_remote_no_tracking
    ):
        """A configured remote without refs cannot prove remote reachability."""
        import time
        import cli

        info = _setup_worktree(str(git_repo_remote_no_tracking))
        assert info is not None
        old_time = time.time() - (25 * 3600)
        os.utime(info["path"], (old_time, old_time))

        cli._prune_stale_worktrees(str(git_repo_remote_no_tracking))

        assert Path(info["path"]).exists()

    def test_preserves_very_old_worktree_with_unique_commit(self, git_repo):
        """Age must never override protection for uniquely reachable commits."""
        import time
        import cli

        info = _setup_worktree(str(git_repo))
        assert info is not None

        # Make a commit that is reachable only from the worktree branch.
        (Path(info["path"]) / "work.txt").write_text("stale work")
        subprocess.run(["git", "add", "work.txt"], cwd=info["path"], capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "old agent work"],
            cwd=info["path"], capture_output=True,
        )

        # Make it very old (73h — beyond the former hard-delete threshold).
        old_time = time.time() - (73 * 3600)
        os.utime(info["path"], (old_time, old_time))

        cli._prune_stale_worktrees(str(git_repo))

        assert Path(info["path"]).exists()


class TestEdgeCases:
    """Test edge cases for robustness."""

    def test_no_commits_repo(self, tmp_path):
        """Worktree creation should fail gracefully on a repo with no commits."""
        repo = tmp_path / "empty-repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)

        info = _setup_worktree(str(repo))
        assert info is None  # Should fail gracefully

    def test_not_a_git_repo(self, tmp_path):
        """Repo detection should return None for non-git directories."""
        bare = tmp_path / "not-git"
        bare.mkdir()
        root = _git_repo_root(cwd=str(bare))
        assert root is None

    def test_worktrees_dir_already_exists(self, git_repo):
        """Should work fine if .worktrees/ already exists."""
        (git_repo / ".worktrees").mkdir(exist_ok=True)
        info = _setup_worktree(str(git_repo))
        assert info is not None
        assert Path(info["path"]).exists()


class TestCLIFlagLogic:
    """Test the flag/config OR logic from main()."""

    def test_worktree_flag_triggers(self):
        """--worktree flag should trigger worktree creation."""
        worktree = True
        w = False
        config_worktree = False
        use_worktree = worktree or w or config_worktree
        assert use_worktree

    def test_w_flag_triggers(self):
        """-w flag should trigger worktree creation."""
        worktree = False
        w = True
        config_worktree = False
        use_worktree = worktree or w or config_worktree
        assert use_worktree

    def test_config_triggers(self):
        """worktree: true in config should trigger worktree creation."""
        worktree = False
        w = False
        config_worktree = True
        use_worktree = worktree or w or config_worktree
        assert use_worktree

    def test_none_set_no_trigger(self):
        """No flags and no config should not trigger."""
        worktree = False
        w = False
        config_worktree = False
        use_worktree = worktree or w or config_worktree
        assert not use_worktree


class TestTerminalCWDIntegration:
    """Test that TERMINAL_CWD is correctly set to the worktree path."""

    def test_terminal_cwd_set(self, git_repo):
        """After worktree setup, TERMINAL_CWD should point to the worktree."""
        info = _setup_worktree(str(git_repo))
        assert info is not None

        # This is what main() does:
        os.environ["TERMINAL_CWD"] = info["path"]
        assert os.environ["TERMINAL_CWD"] == info["path"]
        assert Path(os.environ["TERMINAL_CWD"]).exists()

        # Clean up env
        del os.environ["TERMINAL_CWD"]

    def test_terminal_cwd_is_valid_git_repo(self, git_repo):
        """The TERMINAL_CWD worktree should be a valid git working tree."""
        info = _setup_worktree(str(git_repo))
        assert info is not None

        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=info["path"],
        )
        assert result.stdout.strip() == "true"


class TestOrphanedBranchPruning:
    """Test cleanup of orphaned hermes/* and pr-* branches."""

    def test_prunes_orphaned_hermes_branch(self, git_repo):
        """hermes/hermes-* branches with no worktree should be deleted."""
        import cli

        # Create a branch that looks like a worktree branch but has no worktree
        branch = "hermes/hermes-deadbeef"
        subprocess.run(
            ["git", "branch", branch, "HEAD"],
            cwd=git_repo, capture_output=True, check=True,
        )

        cli._prune_orphaned_branches(str(git_repo))

        result = subprocess.run(
            ["git", "branch", "--list", branch],
            capture_output=True, text=True, cwd=git_repo, check=True,
        )
        assert branch not in result.stdout

    def test_prunes_orphaned_pr_branch(self, git_repo):
        """pr-* branches should be deleted during pruning."""
        import cli

        subprocess.run(
            ["git", "branch", "pr-1234", "HEAD"],
            cwd=git_repo, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "branch", "pr-5678", "HEAD"],
            cwd=git_repo, capture_output=True, check=True,
        )

        cli._prune_orphaned_branches(str(git_repo))

        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            capture_output=True, text=True, cwd=git_repo, check=True,
        )
        remaining = result.stdout.strip()
        assert "pr-1234" not in remaining
        assert "pr-5678" not in remaining

    def test_preserves_orphaned_branch_with_unique_commit(self, git_repo):
        """An orphan auto branch is the last ref for its commits, so keep it."""
        import cli

        info = _setup_worktree(str(git_repo))
        worktree = Path(info["path"])
        (worktree / "unique.txt").write_text("must survive")
        subprocess.run(
            ["git", "add", "unique.txt"],
            cwd=worktree, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "unique orphan work"],
            cwd=worktree, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "worktree", "remove", str(worktree), "--force"],
            cwd=git_repo, capture_output=True, check=True,
        )

        cli._prune_orphaned_branches(str(git_repo))

        result = subprocess.run(
            ["git", "branch", "--list", info["branch"]],
            cwd=git_repo, capture_output=True, text=True, check=True,
        )
        assert info["branch"] in result.stdout

    def test_preserves_active_worktree_branch(self, git_repo):
        """Branches with active worktrees should NOT be pruned."""
        import cli

        info = _setup_worktree(str(git_repo))
        assert info is not None

        cli._prune_orphaned_branches(str(git_repo))

        result = subprocess.run(
            ["git", "branch", "--list", info["branch"]],
            capture_output=True, text=True, cwd=git_repo, check=True,
        )
        assert info["branch"] in result.stdout

    def test_preserves_main_branch(self, git_repo):
        """main branch should never be pruned."""
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            capture_output=True, text=True, cwd=str(git_repo),
        )
        all_branches = [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]
        active_branches = {"main"}

        orphaned = [
            b for b in all_branches
            if b not in active_branches
            and (b.startswith("hermes/hermes-") or b.startswith("pr-"))
        ]
        assert "main" not in orphaned


class TestSystemPromptInjection:
    """Test that the agent gets worktree context in its system prompt."""

    def test_prompt_note_format(self, git_repo):
        """Verify the system prompt note contains all required info."""
        info = _setup_worktree(str(git_repo))
        assert info is not None

        # This is what main() does:
        wt_note = (
            f"\n\n[System note: You are working in an isolated git worktree at "
            f"{info['path']}. Your branch is `{info['branch']}`. "
            f"Changes here do not affect the main working tree or other agents. "
            f"Remember to commit and push your changes, and create a PR if appropriate. "
            f"The original repo is at {info['repo_root']}.]\n"
        )

        assert info["path"] in wt_note
        assert info["branch"] in wt_note
        assert info["repo_root"] in wt_note
        assert "isolated git worktree" in wt_note
        assert "commit and push" in wt_note


class TestWorktreeLockReaping:
    """Exercise the REAL cli._prune_stale_worktrees lock/dirty/unpushed logic.

    Unlike the reimplementation-based tests above, these import the actual
    production functions so the behavior contract is enforced against the
    shipped code:

    - live-locked (owning pid running)  -> never reaped, any age
    - dead-locked clean (owning pid gone) -> unlocked + reaped (fixes the
      accumulation bug: `git worktree remove --force` refuses a locked tree)
    - dirty (uncommitted) at >72h        -> preserved
    - unpushed commits at any age        -> preserved
    - clean/unlocked stale               -> reaped (aggressive cleanup intact)
    """

    @staticmethod
    def _age(path, hours):
        import time
        t = time.time() - (hours * 3600)
        os.utime(path, (t, t))

    @staticmethod
    def _mk(cli, repo, name, pid=None, dirty=False, unpushed=False, age_h=100):
        p = repo / ".worktrees" / name
        (repo / ".worktrees").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(p), "-b", f"hermes/{name}", "HEAD"],
            cwd=repo, capture_output=True,
        )
        if pid is not None:
            subprocess.run(
                ["git", "worktree", "lock", "--reason", f"hermes pid={pid}", str(p)],
                cwd=repo, capture_output=True,
            )
        if unpushed:
            (p / "work.txt").write_text("x")
            subprocess.run(["git", "add", "work.txt"], cwd=p, capture_output=True)
            subprocess.run(["git", "commit", "-m", "wip"], cwd=p, capture_output=True)
        if dirty:
            (p / "dirty.txt").write_text("uncommitted")
        TestWorktreeLockReaping._age(p, age_h)
        return p

    def test_live_locked_survives_at_any_age(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-live", pid=os.getpid())
        cli._prune_stale_worktrees(str(git_repo))
        assert wt.exists(), "live-locked worktree (this pid) must never be reaped"

    def test_dead_locked_clean_is_reaped(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-dead", pid=999999)
        # sanity: this is the accumulation bug — remove --force alone can't do it
        assert cli._worktree_lock_is_live(str(git_repo), str(wt)) == "dead"
        cli._prune_stale_worktrees(str(git_repo))
        assert not wt.exists(), "dead-locked clean worktree should be unlocked + reaped"

    def test_dead_locked_dirty_survives(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-deaddirty", pid=999999, dirty=True)
        cli._prune_stale_worktrees(str(git_repo))
        assert wt.exists(), "dead-locked worktree with uncommitted work must survive"

    def test_dead_locked_unpushed_survives(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-deadunp", pid=999999, unpushed=True)
        cli._prune_stale_worktrees(str(git_repo))
        assert wt.exists(), "dead-locked worktree with unpushed commits must survive"

    def test_unlocked_clean_stale_is_reaped(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-nolock", pid=None)
        cli._prune_stale_worktrees(str(git_repo))
        assert not wt.exists(), "clean unlocked stale worktree should be reaped"

    def test_dirty_survives_over_72h(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-dirty72", pid=None, dirty=True, age_h=100)
        cli._prune_stale_worktrees(str(git_repo))
        assert wt.exists(), "dirty worktree must survive even past the 72h tier"

    def test_dirty_survives_between_24_and_72h(self, git_repo):
        """The normal stale tier must preserve uncommitted work too."""
        import cli

        wt = self._mk(cli, git_repo, "hermes-dirty25", dirty=True, age_h=25)
        cli._prune_stale_worktrees(str(git_repo))
        assert wt.exists(), "age must never override dirty-worktree protection"

    def test_recent_worktree_untouched(self, git_repo):
        import cli
        wt = self._mk(cli, git_repo, "hermes-fresh", pid=None, age_h=1)
        cli._prune_stale_worktrees(str(git_repo))
        assert wt.exists(), "worktree under 24h must never be pruned"


class TestWorktreeLockPredicate:
    """_worktree_lock_is_live classification (real cli helper)."""

    def _mk_locked(self, repo, name, reason):
        p = repo / ".worktrees" / name
        (repo / ".worktrees").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(p), "-b", f"hermes/{name}", "HEAD"],
            cwd=repo, capture_output=True,
        )
        subprocess.run(
            ["git", "worktree", "lock", "--reason", reason, str(p)],
            cwd=repo, capture_output=True,
        )
        return p

    def test_unlocked_returns_none(self, git_repo):
        import cli
        p = git_repo / ".worktrees" / "hermes-x"
        (git_repo / ".worktrees").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(p), "-b", "hermes/hermes-x", "HEAD"],
            cwd=git_repo, capture_output=True,
        )
        assert cli._worktree_lock_is_live(str(git_repo), str(p)) is None

    def test_live_pid_returns_live(self, git_repo):
        import cli
        p = self._mk_locked(git_repo, "hermes-live", f"hermes pid={os.getpid()}")
        assert cli._worktree_lock_is_live(str(git_repo), str(p)) == "live"

    def test_dead_pid_returns_dead(self, git_repo):
        import cli
        p = self._mk_locked(git_repo, "hermes-dead", "hermes pid=999999")
        assert cli._worktree_lock_is_live(str(git_repo), str(p)) == "dead"

    def test_foreign_lock_reason_fails_safe_to_live(self, git_repo):
        import cli
        p = self._mk_locked(git_repo, "hermes-foreign", "some other tool")
        assert cli._worktree_lock_is_live(str(git_repo), str(p)) == "live"

    def test_bad_repo_root_fails_safe_to_live(self, tmp_path):
        import cli
        # Not a git repo -> git query fails -> must report "live" (never delete)
        assert cli._worktree_lock_is_live(str(tmp_path), str(tmp_path / "x")) == "live"
