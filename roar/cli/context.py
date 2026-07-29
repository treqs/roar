"""
Click context extension for roar CLI.

Provides RoarContext dataclass that holds roar-specific data
passed through the Click command chain via ctx.obj.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SENTINEL: dict[str, Any] = {}


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
        config: Configuration dictionary (lazy-loaded on first access)
    """

    roar_dir: Path
    repo_root: Path | None
    cwd: Path
    is_interactive: bool
    _config: dict[str, Any] = field(default_factory=lambda: _SENTINEL, repr=False)

    @property
    def config(self) -> dict[str, Any]:
        """Lazy-loaded configuration. Only imports config machinery on first access."""
        if self._config is _SENTINEL:
            if self.roar_dir.exists():
                self._config = self._load_config(self.roar_dir.parent)
            else:
                self._config = {}
        return self._config

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self._config = value

    @classmethod
    def create(cls, cwd: Path | None = None) -> RoarContext:
        """Create a RoarContext for the current environment.

        This factory method gathers all necessary context information:
        - Determines the .roar directory location
        - Finds the git repository root (if any)
        - Configuration is lazy-loaded on first access to .config

        Args:
            cwd: Working directory override (defaults to Path.cwd())

        Returns:
            Configured RoarContext instance
        """
        if cwd is None:
            cwd = Path.cwd()

        # ROAR_PROJECT_DIR pins the project explicitly — the same contract
        # the config loaders and fragment planners already honor
        # (resolve_project_roar_dir). Gated on the directory existing so
        # environments that propagate a host path into a pod (Ray worker
        # env) fall back to the cwd walk instead of a phantom project.
        override = str(os.environ.get("ROAR_PROJECT_DIR") or "").strip()
        if override and Path(override).is_dir():
            project_dir = Path(override)
            return cls(
                roar_dir=project_dir / ".roar",
                repo_root=cls._get_repo_root(project_dir),
                cwd=cwd,
                is_interactive=sys.stdin.isatty(),
            )

        # Get VCS provider and find repo root.
        repo_root = cls._get_repo_root(cwd)

        # Walk upward looking for .roar directory, bounded to workspace.
        # Inlined to avoid importing roar.core (heavy cascade) at CLI init.
        stop = (repo_root or cwd).resolve()
        roar_dir = cwd / ".roar"  # default fallback
        for parent in [cwd, *list(cwd.parents)]:
            candidate = parent / ".roar"
            if candidate.is_dir():
                roar_dir = candidate
                break
            if parent.resolve() == stop:
                break

        return cls(
            roar_dir=roar_dir,
            repo_root=repo_root,
            cwd=cwd,
            is_interactive=sys.stdin.isatty(),
        )

    @staticmethod
    def _get_repo_root(start_dir: Path | None = None) -> Path | None:
        """Get the git repository root, if in a git repo.

        Uses git directly to avoid importing the container (and its heavy
        dependency chain) before bootstrap.

        Returns:
            Path to repo root, or None if not in a git repository
        """
        try:
            import subprocess

            out = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
                cwd=start_dir,
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
            from ..integrations.config import load_config

            return load_config(start_dir=str(start_dir) if start_dir else None)
        except Exception:
            return {}

    @property
    def is_initialized(self) -> bool:
        """Check if roar is initialized (has .roar directory)."""
        return self.roar_dir.exists()

    @property
    def has_repo(self) -> bool:
        """Check if we're in a git repository.

        Returns True when ROAR_JOB_INSTRUMENTED=1 (Ray job driver running
        from an extracted working_dir that is not a git repo).
        """
        if self.repo_root is not None:
            return True
        # Ray job drivers run from extracted working dirs without .git
        return os.environ.get("ROAR_JOB_INSTRUMENTED") == "1"
