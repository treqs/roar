"""
Shared execution helpers for run and build commands.

This module extracts common logic used by both `roar run` and `roar build`
commands, following the DRY principle. Both commands share ~70% of their
implementation: git validation, quiet setting resolution, execution
coordination, and result reporting.

Usage:
    from ._execution import validate_git_clean, get_quiet_setting, execute_and_report
"""

from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ..context import RoarContext


def validate_git_clean() -> tuple[str, dict]:
    """
    Validate git repository is clean and return git info.

    This function checks:
    1. We're inside a git repository
    2. The working tree has no uncommitted changes

    Returns:
        Tuple of (repo_root, git_info) where git_info contains:
        - commit: Current commit hash
        - branch: Current branch name
        - remote_url: Remote origin URL

    Raises:
        click.ClickException: If not in a git repo or has uncommitted changes
    """
    from ...core.bootstrap import bootstrap
    from ...core.container import get_container

    # Bootstrap container to ensure VCS provider is registered
    bootstrap()

    vcs = get_container().get_vcs_provider("git")
    repo_root = vcs.get_repo_root()

    if not repo_root:
        raise click.ClickException(
            "roar requires the working directory to be inside a git repository."
        )

    clean, changes = vcs.get_status(repo_root)
    if not clean:
        lines = ["Git repo has uncommitted changes:"]
        for change in changes[:5]:
            lines.append(f"  {change}")
        if len(changes) > 5:
            lines.append(f"  ... and {len(changes) - 5} more")
        lines.append("")
        lines.append("Commit your changes before running this command.")
        raise click.ClickException("\n".join(lines))

    # Get git info
    vcs_info = vcs.get_info(repo_root)
    git_info = {
        "commit": vcs_info.commit if vcs_info else None,
        "branch": vcs_info.branch if vcs_info else None,
        "remote_url": vcs_info.remote_url if vcs_info else None,
    }

    return repo_root, git_info


def get_quiet_setting(quiet_flag: bool | None, repo_root: str | Path) -> bool:
    """
    Get quiet setting from CLI flag or config.

    The CLI flag takes precedence. If not provided, checks the config
    for `output.quiet` setting.

    Args:
        quiet_flag: Explicit quiet flag from command line (None if not specified)
        repo_root: Repository root for config lookup

    Returns:
        Whether to use quiet mode
    """
    if quiet_flag is not None:
        return quiet_flag

    from ...config import load_config

    config = load_config(start_dir=str(repo_root) if repo_root else None)
    return config.get("output", {}).get("quiet", False)


def execute_and_report(
    ctx: "RoarContext",
    command: list[str],
    job_type: str | None,
    quiet: bool,
    hash_algorithms: list[str],
    git_info: dict,
    repo_root: str,
) -> int:
    """
    Execute command via coordinator and show report.

    This is the core execution function shared between run and build.
    It handles:
    1. Creating the RunContext
    2. Executing via RunCoordinator
    3. Showing the result report
    4. Displaying stale step warnings

    Args:
        ctx: RoarContext with roar_dir and other context
        command: Command to execute as list of strings
        job_type: Job type - None for run, "build" for build
        quiet: Whether to suppress output
        hash_algorithms: List of hash algorithms to use
        git_info: Git info dict with commit, branch, remote_url
        repo_root: Git repository root path

    Returns:
        Exit code from the executed command
    """
    from typing import Literal, cast

    from ...core.interfaces.run import RunContext
    from ...presenters.run_report import RunReportPresenter
    from ...services.execution import RunCoordinator

    # Create run context
    hash_algos = cast(list[Literal["blake3", "sha256", "sha512", "md5"]], hash_algorithms)
    job_type_literal = cast(Literal["run", "build"] | None, job_type)
    run_ctx = RunContext(
        roar_dir=ctx.roar_dir,
        repo_root=repo_root,
        command=command,
        job_type=job_type_literal,
        quiet=quiet,
        hash_algorithms=hash_algos,
        git_commit=git_info.get("commit"),
        git_branch=git_info.get("branch"),
        git_repo=git_info.get("remote_url"),
    )

    # Execute via coordinator
    coordinator = RunCoordinator()
    result = coordinator.execute(run_ctx)

    # Present report
    from ...presenters.console import ConsolePresenter

    presenter = ConsolePresenter()
    report = RunReportPresenter(presenter)
    report.show_report(result, command, quiet)

    # Show stale warnings
    if result.stale_upstream or result.stale_downstream:
        report.show_stale_warnings(
            result.stale_upstream,
            result.stale_downstream,
            is_build=(job_type == "build"),
        )

    return result.exit_code


def get_hash_algorithms(cli_algorithms: list[str] | None = None) -> list[str]:
    """
    Get hash algorithms from CLI or config.

    Args:
        cli_algorithms: Algorithms specified on command line

    Returns:
        List of hash algorithm names
    """
    from ...config import get_hash_algorithms as config_get_hash_algorithms

    return config_get_hash_algorithms(
        operation="run",
        cli_algorithms=cli_algorithms if cli_algorithms else None,
        hash_only=False,
    )
