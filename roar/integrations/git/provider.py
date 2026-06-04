"""
Git VCS provider.

Implements VCS operations for Git repositories.
"""

import contextlib
import subprocess
from pathlib import Path

from ...core.models.vcs import VCSInfo
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
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def get_info(self, repo_root: str) -> VCSInfo:
        """Get comprehensive git repository information."""
        info = VCSInfo()
        if not self.is_available():
            return info

        # Current commit hash
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            info.commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )

        # Current branch
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            info.branch = (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=repo_root,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )

        # Remote URL — canonical remote (configured/origin/sole), not assumed
        # to be named "origin", so a repo whose only remote is e.g. "treqs"
        # records the real URL instead of falling back to a local file:// path.
        info.remote_url = self.get_remote_url(repo_root)

        # Check for uncommitted changes
        clean, changes = self.get_status(repo_root)
        info.clean = clean
        if not clean:
            info.uncommitted_changes = changes

        # Commit timestamp
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
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
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
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
        """Get the git working tree status.

        ``stderr`` is suppressed so that probing a non-git directory (now
        that ``roar run`` works outside a repo) doesn't leak git's
        ``fatal: not a git repository`` chatter onto the user's terminal —
        the ``CalledProcessError`` is the signal we act on.
        """
        try:
            out = subprocess.check_output(
                ["git", "status", "--porcelain=v1"], cwd=repo_root, stderr=subprocess.DEVNULL
            )
            lines = out.decode().splitlines()
            clean = len(lines) == 0
            return clean, lines
        except (subprocess.CalledProcessError, FileNotFoundError):
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
        except (subprocess.CalledProcessError, FileNotFoundError):
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
        except (subprocess.CalledProcessError, FileNotFoundError):
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
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def list_remotes(self, repo_root: str) -> list[str]:
        """Names of the configured git remotes (empty if none / not a repo)."""
        try:
            raw = subprocess.check_output(
                ["git", "remote"], cwd=repo_root, stderr=subprocess.DEVNULL
            ).decode()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def resolve_remote_name(self, repo_root: str, configured: str | None = None) -> str | None:
        """Pick the remote whose URL best identifies this repo.

        Mirrors the tag-push canonical-remote heuristic so the recorded
        ``git_repo`` matches where tags are actually pushed: the configured
        ``git.remote`` if present, else ``origin``, else the sole remote when
        there's exactly one (e.g. a repo whose only remote is named ``treqs``).
        Returns ``None`` when it's genuinely ambiguous (multiple remotes, no
        ``origin``, none configured) so the caller can fall back.
        """
        remotes = self.list_remotes(repo_root)
        if configured and configured in remotes:
            return configured
        if "origin" in remotes:
            return "origin"
        if len(remotes) == 1:
            return remotes[0]
        return None

    def get_remote_url(
        self, repo_root: str, remote: str | None = None, configured: str | None = None
    ) -> str | None:
        """Get the URL for a remote.

        With an explicit ``remote`` name, fetches that one. Otherwise resolves
        the canonical remote via :meth:`resolve_remote_name` (honoring an
        optional ``configured`` ``git.remote``) instead of assuming ``origin``.
        """
        name = remote or self.resolve_remote_name(repo_root, configured)
        if name is None:
            return None
        try:
            return (
                subprocess.check_output(
                    ["git", "remote", "get-url", name], cwd=repo_root, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def create_tag(
        self,
        repo_root: str,
        tag_name: str,
        message: str | None = None,
        target: str | None = None,
    ) -> tuple[bool, str | None]:
        """Create a lightweight or annotated git tag.

        Args:
            repo_root: Path to the git repository root
            tag_name: Name of the tag to create
            message: Optional message for annotated tag. If None, creates lightweight tag.
            target: Optional commit-ish to tag. Defaults to HEAD when omitted.
                Pass an explicit SHA when the call site cares about WHICH
                commit gets tagged (e.g. tagging a historical job commit
                that may differ from HEAD).

        Returns:
            Tuple of (success, error_message). error_message is None on success.
        """
        try:
            cmd = ["git", "tag"]
            if message:
                cmd.extend(["-a", tag_name, "-m", message])
            else:
                cmd.append(tag_name)
            if target:
                cmd.append(target)

            subprocess.check_output(cmd, cwd=repo_root, stderr=subprocess.STDOUT)
            return True, None
        except subprocess.CalledProcessError as e:
            error_msg = e.output.decode().strip() if e.output else str(e)
            return False, error_msg
