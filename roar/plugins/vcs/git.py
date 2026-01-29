"""
Git VCS provider.

Implements VCS operations for Git repositories.
"""

import contextlib
import subprocess
from pathlib import Path

from ...core.interfaces.vcs import VCSInfo
from .base import BaseVCSProvider


class GitVCSProvider(BaseVCSProvider):
    """
    Git version control provider.

    Provides Git-specific implementations for repository information,
    status checking, and file classification.
    """

    @property
    def name(self) -> str:
        return "git"

    def is_available(self) -> bool:
        """Check if git is installed."""
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def get_repo_root(self, path: str | None = None) -> str | None:
        """Get the git repository root directory."""
        try:
            cmd = ["git", "rev-parse", "--show-toplevel"]
            if path:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, cwd=path)
            else:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
            return out.decode().strip()
        except subprocess.CalledProcessError:
            return None

    def get_info(self, repo_root: str) -> VCSInfo:
        """Get comprehensive git repository information."""
        info = VCSInfo()

        # Current commit hash
        with contextlib.suppress(subprocess.CalledProcessError):
            info.commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )

        # Current branch
        with contextlib.suppress(subprocess.CalledProcessError):
            info.branch = (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=repo_root,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )

        # Remote URL (origin)
        with contextlib.suppress(subprocess.CalledProcessError):
            info.remote_url = (
                subprocess.check_output(
                    ["git", "remote", "get-url", "origin"], cwd=repo_root, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )

        # Check for uncommitted changes
        clean, changes = self.get_status(repo_root)
        info.clean = clean
        if not clean:
            info.uncommitted_changes = changes

        # Commit timestamp
        with contextlib.suppress(subprocess.CalledProcessError):
            info.commit_timestamp = (
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%ci", "HEAD"],
                    cwd=repo_root,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )

        # Commit message (first line)
        with contextlib.suppress(subprocess.CalledProcessError):
            info.commit_message = (
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%s", "HEAD"],
                    cwd=repo_root,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )

        return info

    def get_status(self, repo_root: str) -> tuple[bool, list[str]]:
        """Get the git working tree status."""
        try:
            out = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo_root)
            lines = out.decode().splitlines()
            clean = len(lines) == 0
            return clean, lines
        except subprocess.CalledProcessError:
            return True, []

    def classify_file(self, repo_root: str, path: str) -> str:
        """
        Classify a file relative to the git repository.

        Returns:
            'tracked' - File is tracked by git
            'untracked' - File is in repo but not tracked
            'site-package' - File is in site-packages directory
            'external' - File is outside the repository
        """
        path_obj = Path(path).resolve()
        repo_root_obj = Path(repo_root).resolve()

        # Check if path is inside the repo
        try:
            rel = path_obj.relative_to(repo_root_obj)
        except ValueError:
            # Path is outside the repo
            if "site-packages" in str(path_obj):
                return "site-package"
            return "external"

        # Path is inside repo, check if it's tracked
        if self.is_tracked(repo_root, str(rel)):
            return "tracked"
        return "untracked"

    def is_tracked(self, repo_root: str, path: str) -> bool:
        """Check if a file is tracked by git."""
        try:
            subprocess.check_output(
                ["git", "ls-files", "--error-unmatch", str(path)],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def get_commit_hash(self, repo_root: str) -> str | None:
        """Get the current commit hash."""
        try:
            return (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
        except subprocess.CalledProcessError:
            return None

    def get_branch(self, repo_root: str) -> str | None:
        """Get the current branch name."""
        try:
            return (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=repo_root,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
        except subprocess.CalledProcessError:
            return None

    def get_remote_url(self, repo_root: str, remote: str = "origin") -> str | None:
        """Get the URL for a remote."""
        try:
            return (
                subprocess.check_output(
                    ["git", "remote", "get-url", remote], cwd=repo_root, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
        except subprocess.CalledProcessError:
            return None

    def create_tag(
        self, repo_root: str, tag_name: str, message: str | None = None
    ) -> tuple[bool, str | None]:
        """Create a lightweight or annotated git tag.

        Args:
            repo_root: Path to the git repository root
            tag_name: Name of the tag to create
            message: Optional message for annotated tag. If None, creates lightweight tag.

        Returns:
            Tuple of (success, error_message). error_message is None on success.
        """
        try:
            cmd = ["git", "tag"]
            if message:
                cmd.extend(["-a", tag_name, "-m", message])
            else:
                cmd.append(tag_name)

            subprocess.check_output(cmd, cwd=repo_root, stderr=subprocess.STDOUT)
            return True, None
        except subprocess.CalledProcessError as e:
            error_msg = e.output.decode().strip() if e.output else str(e)
            return False, error_msg
