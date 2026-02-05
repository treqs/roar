"""
Git operations for roar put command.

Handles tag creation and push for artifact publishing.
"""

import subprocess
from pathlib import Path


class GitError(Exception):
    """Error during git operation."""

    pass


class GitOperations:
    """
    Git operations for artifact publishing.

    Handles creating and pushing tags to mark published artifacts.
    """

    def __init__(self, repo_root: Path | None = None):
        """
        Initialize git operations.

        Args:
            repo_root: Path to git repository root.
        """
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """
        Run a git command.

        Args:
            *args: Git command arguments.
            check: Whether to raise on non-zero exit.

        Returns:
            CompletedProcess result.

        Raises:
            GitError: If command fails and check=True.
        """
        result = subprocess.run(
            ["git", *args],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            raise GitError(error_msg)
        return result

    def get_current_commit(self) -> str:
        """
        Get the current HEAD commit SHA.

        Returns:
            40-character commit SHA.

        Raises:
            GitError: If not in a git repository.
        """
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def create_tag(self, commit: str) -> str:
        """
        Create a roar tag for the given commit.

        Tag format: roar/<commit-sha>
        This is idempotent - creating the same tag twice is a no-op.

        Args:
            commit: Commit SHA to tag.

        Returns:
            Tag name that was created.
        """
        tag_name = f"roar/{commit}"

        # Check if tag already exists
        result = self._run_git("tag", "-l", tag_name, check=False)
        if tag_name in result.stdout:
            return tag_name

        # Create the tag
        self._run_git("tag", tag_name, commit)
        return tag_name

    def has_uncommitted_changes(self) -> bool:
        """
        Check if the repository has uncommitted changes.

        Returns:
            True if there are staged or unstaged changes.
        """
        # Check for any changes (staged or unstaged)
        result = self._run_git("status", "--porcelain", check=False)
        return bool(result.stdout.strip())

    def push_tag(self, tag_name: str, remote: str = "origin") -> None:
        """
        Push a tag to a remote.

        Args:
            tag_name: Tag to push.
            remote: Remote name (default: origin).

        Raises:
            GitError: If push fails.
        """
        self._run_git("push", remote, tag_name)

    def get_remote_url(self, remote: str = "origin") -> str | None:
        """
        Get the URL of a remote.

        Args:
            remote: Remote name (default: origin).

        Returns:
            Remote URL or None if not configured.
        """
        result = self._run_git("remote", "get-url", remote, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
