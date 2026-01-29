"""
Unified execution service for run and build commands.

This service extracts the common logic between run.py and build.py,
eliminating ~150 lines of duplication.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.container import get_container
from ...core.interfaces.run import RunContext, RunResult
from .coordinator import RunCoordinator

if TYPE_CHECKING:
    from ...core.interfaces.presenter import IPresenter
    from ...core.interfaces.vcs import IVCSProvider


@dataclass
class GitValidationResult:
    """Result of git repository validation."""

    is_valid: bool
    error_message: str | None = None
    repo_root: str | None = None
    commit: str | None = None
    branch: str | None = None
    remote_url: str | None = None
    uncommitted_changes: list[str] | None = None


@dataclass
class ExecutionRequest:
    """Request parameters for command execution."""

    roar_dir: Path
    command: list[str]
    job_type: str | None = None  # None for run, "build" for build
    quiet: bool | None = None
    hash_algorithms: list[str] | None = None


class ExecutionService:
    """
    Unified execution service for run and build commands.

    This service encapsulates:
    - Git validation (clean working tree check)
    - Git info retrieval (commit, branch, remote)
    - Config loading (quiet setting)
    - Execution coordination
    - Result reporting

    Usage:
        service = ExecutionService()
        result = service.execute(ExecutionRequest(...))
    """

    def __init__(
        self,
        coordinator: RunCoordinator | None = None,
        presenter: "IPresenter | None" = None,
        vcs_provider: "IVCSProvider | None" = None,
    ) -> None:
        """
        Initialize execution service.

        Args:
            coordinator: Run coordinator (created lazily if not provided)
            presenter: Output presenter
            vcs_provider: VCS provider for git operations
        """
        self._coordinator = coordinator
        self._presenter = presenter
        self._vcs_provider = vcs_provider

    def validate_git(self, require_clean: bool = True) -> GitValidationResult:
        """
        Validate git repository state.

        Args:
            require_clean: Whether to require a clean working tree

        Returns:
            GitValidationResult with validation status and git info
        """
        vcs = self._get_vcs()
        repo_root = vcs.get_repo_root()

        if not repo_root:
            return GitValidationResult(
                is_valid=False,
                error_message="roar requires the working directory to be inside a git repository.",
            )

        # Check for clean working tree if required
        if require_clean:
            clean, changes = vcs.get_status(repo_root)
            if not clean:
                return GitValidationResult(
                    is_valid=False,
                    error_message="Git repo has uncommitted changes",
                    repo_root=repo_root,
                    uncommitted_changes=changes,
                )

        # Get git info
        vcs_info = vcs.get_info(repo_root)

        return GitValidationResult(
            is_valid=True,
            repo_root=repo_root,
            commit=vcs_info.commit if vcs_info else None,
            branch=vcs_info.branch if vcs_info else None,
            remote_url=vcs_info.remote_url if vcs_info else None,
        )

    def load_quiet_setting(self, repo_root: str | Path, explicit_quiet: bool | None) -> bool:
        """
        Load quiet setting from explicit arg or config.

        Args:
            repo_root: Repository root for config lookup
            explicit_quiet: Explicit quiet setting from command line

        Returns:
            Whether to use quiet mode
        """
        if explicit_quiet is not None:
            return explicit_quiet

        from ...config import load_config

        config = load_config(start_dir=str(repo_root) if repo_root else None)
        return config.get("output", {}).get("quiet", False)

    def execute(
        self,
        request: ExecutionRequest,
        git_info: GitValidationResult | None = None,
    ) -> RunResult:
        """
        Execute a command with provenance tracking.

        Args:
            request: Execution request parameters
            git_info: Pre-validated git info (validates if not provided)

        Returns:
            RunResult with execution results

        Raises:
            ValueError: If git validation fails
        """
        # Validate git if not already done
        if git_info is None:
            git_info = self.validate_git(require_clean=True)

        if not git_info.is_valid:
            raise ValueError(git_info.error_message or "Git validation failed")

        # Load quiet setting
        repo_root = git_info.repo_root or ""
        quiet = self.load_quiet_setting(repo_root, request.quiet)

        # Create execution context
        # Note: hash_algorithms defaults to ["blake3"] in RunContext if not specified
        from typing import Literal, cast

        hash_algos = cast(
            list[Literal["blake3", "sha256", "sha512", "md5"]],
            request.hash_algorithms or ["blake3"],
        )
        job_type = cast(Literal["run", "build"] | None, request.job_type)
        run_ctx = RunContext(
            roar_dir=request.roar_dir,
            repo_root=repo_root,
            command=request.command,
            job_type=job_type,
            quiet=quiet,
            hash_algorithms=hash_algos,
            git_commit=git_info.commit,
            git_branch=git_info.branch,
            git_repo=git_info.remote_url,
        )

        # Execute via coordinator
        coordinator = self._get_coordinator()
        return coordinator.execute(run_ctx)

    def _get_vcs(self) -> "IVCSProvider":
        """Get VCS provider."""
        if self._vcs_provider:
            return self._vcs_provider
        return get_container().get_vcs_provider("git")

    def _get_coordinator(self) -> RunCoordinator:
        """Get run coordinator, creating if needed."""
        if self._coordinator:
            return self._coordinator
        return RunCoordinator(presenter=self._presenter)
