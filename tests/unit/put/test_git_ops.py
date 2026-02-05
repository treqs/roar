"""
Unit tests for git operations in roar put.

Tests tag creation and push functionality.
"""

import subprocess
from pathlib import Path

import pytest

from roar.services.put.git import GitError, GitOperations


class TestGetCurrentCommit:
    """Tests for getting current git commit."""

    def test_get_current_commit_returns_sha(self, tmp_path: Path):
        """Get current commit returns the HEAD sha."""
        # Create a git repo with a commit
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )

        git_ops = GitOperations(repo_root=tmp_path)

        commit = git_ops.get_current_commit()

        assert len(commit) == 40  # SHA-1 hash length
        assert all(c in "0123456789abcdef" for c in commit)

    def test_get_current_commit_not_a_repo_raises(self, tmp_path: Path):
        """Get current commit raises when not in a git repo."""
        git_ops = GitOperations(repo_root=tmp_path)

        with pytest.raises(GitError) as exc_info:
            git_ops.get_current_commit()

        assert "not a git repository" in str(exc_info.value).lower()


class TestCreateTag:
    """Tests for creating git tags."""

    def test_create_tag_creates_roar_tag(self, tmp_path: Path):
        """Create tag creates a roar/<sha> tag."""
        # Setup git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )

        git_ops = GitOperations(repo_root=tmp_path)
        commit = git_ops.get_current_commit()

        tag_name = git_ops.create_tag(commit)

        assert tag_name == f"roar/{commit}"
        # Verify tag exists
        result = subprocess.run(
            ["git", "tag", "-l", tag_name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert tag_name in result.stdout

    def test_create_tag_idempotent(self, tmp_path: Path):
        """Creating the same tag twice doesn't error."""
        # Setup git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )

        git_ops = GitOperations(repo_root=tmp_path)
        commit = git_ops.get_current_commit()

        # Create tag twice - should not raise
        tag1 = git_ops.create_tag(commit)
        tag2 = git_ops.create_tag(commit)

        assert tag1 == tag2


class TestHasUncommittedChanges:
    """Tests for detecting uncommitted changes."""

    def test_clean_repo_returns_false(self, tmp_path: Path):
        """Clean repo returns False for uncommitted changes."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )

        git_ops = GitOperations(repo_root=tmp_path)

        assert git_ops.has_uncommitted_changes() is False

    def test_dirty_repo_returns_true(self, tmp_path: Path):
        """Dirty repo returns True for uncommitted changes."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )
        # Make it dirty
        (tmp_path / "file.txt").write_text("modified")

        git_ops = GitOperations(repo_root=tmp_path)

        assert git_ops.has_uncommitted_changes() is True

    def test_staged_changes_returns_true(self, tmp_path: Path):
        """Staged but uncommitted changes returns True."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )
        # Stage a change
        (tmp_path / "file.txt").write_text("modified")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)

        git_ops = GitOperations(repo_root=tmp_path)

        assert git_ops.has_uncommitted_changes() is True
