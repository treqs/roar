"""
Version control system provider interface definitions.

Enables pluggable VCS backends (Git, Mercurial, etc.)
following the Open/Closed Principle.
"""

from abc import ABC, abstractmethod

# Re-export models for backward compatibility
from roar.core.models.vcs import VCSInfo


class IVCSProvider(ABC):
    """
    Interface for version control system operations.

    Implementations handle VCS-specific operations while
    conforming to this common interface.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        VCS identifier.

        Examples: 'git', 'hg', 'svn'
        """
        pass

    @abstractmethod
    def get_repo_root(self, path: str | None = None) -> str | None:
        """
        Find the repository root from path.

        Args:
            path: Directory to start searching from (default: cwd)

        Returns:
            Path to repo root, or None if not in a repository
        """
        pass

    @abstractmethod
    def get_info(self, repo_root: str) -> VCSInfo:
        """
        Get comprehensive repository information.

        Args:
            repo_root: Path to repository root

        Returns:
            VCSInfo with commit, branch, remote, status
        """
        pass

    @abstractmethod
    def get_status(self, repo_root: str) -> tuple[bool, list[str]]:
        """
        Get working directory status.

        Args:
            repo_root: Path to repository root

        Returns:
            (is_clean, list_of_changes)
        """
        pass

    @abstractmethod
    def is_tracked(self, repo_root: str, path: str) -> bool:
        """
        Check if a file is tracked by version control.

        Args:
            repo_root: Path to repository root
            path: Path to file (absolute or relative)

        Returns:
            True if tracked, False otherwise
        """
        pass

    @abstractmethod
    def classify_file(
        self,
        repo_root: str,
        path: str,
    ) -> str:
        """
        Classify a file relative to the repository.

        Args:
            repo_root: Path to repository root
            path: Path to file

        Returns:
            One of: "tracked", "untracked", "external", "site-package"
        """
        pass

    def is_available(self) -> bool:
        """
        Check if this VCS is available on the system.

        Returns:
            True if the VCS tool is installed
        """
        return True
