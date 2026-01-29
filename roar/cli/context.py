"""
Click context extension for roar CLI.

Provides RoarContext dataclass that holds roar-specific data
passed through the Click command chain via ctx.obj.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.interfaces.vcs import IVCSProvider


@dataclass
class RoarContext:
    """Extended context passed through Click command chain.

    This dataclass holds roar-specific data that commands need access to.
    It is created once at CLI startup and passed to commands via Click's
    ctx.obj mechanism.

    Attributes:
        roar_dir: Path to .roar directory (may not exist if not initialized)
        repo_root: Path to git repository root (None if not in a repo)
        cwd: Current working directory
        is_interactive: Whether stdin is a TTY (for prompts)
        config: Loaded configuration dictionary
    """

    roar_dir: Path
    repo_root: Path | None
    cwd: Path
    is_interactive: bool
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, cwd: Path | None = None) -> RoarContext:
        """Create a RoarContext for the current environment.

        This factory method gathers all necessary context information:
        - Determines the .roar directory location
        - Finds the git repository root (if any)
        - Loads configuration (if initialized)

        Args:
            cwd: Working directory override (defaults to Path.cwd())

        Returns:
            Configured RoarContext instance
        """
        if cwd is None:
            cwd = Path.cwd()

        # Determine .roar directory
        roar_dir = cwd / ".roar"

        # Get VCS provider and find repo root
        repo_root = cls._get_repo_root()

        # Load config if roar is initialized
        config: dict[str, Any] = {}
        if roar_dir.exists():
            config = cls._load_config(cwd)

        return cls(
            roar_dir=roar_dir,
            repo_root=repo_root,
            cwd=cwd,
            is_interactive=sys.stdin.isatty(),
            config=config,
        )

    @staticmethod
    def _get_repo_root() -> Path | None:
        """Get the git repository root, if in a git repo.

        Returns:
            Path to repo root, or None if not in a git repository
        """
        try:
            from ..core.container import get_container

            container = get_container()
            vcs: IVCSProvider = container.get_vcs_provider("git")
            root = vcs.get_repo_root()
            return Path(root) if root else None
        except Exception:
            # Container not bootstrapped yet — fall back to direct git call
            try:
                import subprocess

                out = subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL,
                )
                return Path(out.decode().strip())
            except Exception:
                return None

    @staticmethod
    def _load_config(start_dir: Path) -> dict[str, Any]:
        """Load roar configuration.

        Args:
            start_dir: Directory to start searching for config

        Returns:
            Configuration dictionary (empty if not found/error)
        """
        try:
            from ..config import load_config

            return load_config(start_dir=str(start_dir) if start_dir else None)
        except Exception:
            return {}

    @property
    def is_initialized(self) -> bool:
        """Check if roar is initialized (has .roar directory)."""
        return self.roar_dir.exists()

    @property
    def has_repo(self) -> bool:
        """Check if we're in a git repository."""
        return self.repo_root is not None
