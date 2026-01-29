"""
Base VCS provider.

Defines the interface for version control system providers.
"""

from abc import abstractmethod

from ...core.interfaces.vcs import IVCSProvider, VCSInfo


class BaseVCSProvider(IVCSProvider):
    """
    Abstract base class for VCS providers.

    Implements the Strategy pattern for version control operations.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the VCS name (e.g., 'git', 'hg')."""
        pass

    @abstractmethod
    def get_repo_root(self, path: str | None = None) -> str | None:
        """
        Get the root directory of the repository.

        Args:
            path: Starting path (defaults to current directory)

        Returns:
            Repository root path, or None if not in a repository
        """
        pass

    @abstractmethod
    def get_info(self, repo_root: str) -> VCSInfo:
        """
        Get comprehensive VCS info for a repository.

        Args:
            repo_root: Path to the repository root

        Returns:
            VCSInfo with commit, branch, status, etc.
        """
        pass

    @abstractmethod
    def get_status(self, repo_root: str) -> tuple[bool, list[str]]:
        """
        Get the working tree status.

        Args:
            repo_root: Path to the repository root

        Returns:
            (is_clean, list_of_changes)
        """
        pass

    @abstractmethod
    def classify_file(self, repo_root: str, path: str) -> str:
        """
        Classify a file relative to the repository.

        Args:
            repo_root: Path to the repository root
            path: Path to the file

        Returns:
            Classification string: 'tracked', 'untracked', 'external', 'site-package'
        """
        pass

    @abstractmethod
    def is_tracked(self, repo_root: str, path: str) -> bool:
        """
        Check if a file is tracked by the VCS.

        Args:
            repo_root: Path to the repository root
            path: Path to the file (relative or absolute)

        Returns:
            True if the file is tracked
        """
        pass

    def is_available(self) -> bool:
        """
        Check if this VCS is available on the system.

        Returns:
            True if the VCS tool is installed
        """
        return True
