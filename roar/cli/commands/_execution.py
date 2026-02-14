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
    Validate git repository is clean and return repo root.

    Lightweight pre-fork check using git subprocesses directly, avoiding
    the heavy bootstrap/import cascade. Git info (commit, branch, etc.)
    is collected separately after the fork via :func:`collect_git_info`.

    Returns:
        Tuple of (repo_root, git_info_placeholder)

    Raises:
        click.ClickException: If not in a git repo or has uncommitted changes
    """
    import subprocess

    # Find repo root (no bootstrap needed)
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise click.ClickException(
            "roar requires the working directory to be inside a git repository."
        )

    # Check dirty status
    try:
        status_output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=repo_root,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        status_output = ""

    if status_output:
        changes = status_output.split("\n")
        lines = ["Git repo has uncommitted changes:"]
        for change in changes[:5]:
            lines.append(f"  {change}")
        if len(changes) > 5:
            lines.append(f"  ... and {len(changes) - 5} more")
        lines.append("")
        lines.append("Commit your changes before running this command.")
        raise click.ClickException("\n".join(lines))

    # Return empty git_info — collected later via collect_git_info()
    return repo_root, {}


def collect_git_info(repo_root: str) -> dict:
    """
    Collect git metadata (commit, branch, remote) for provenance.

    This is separated from validate_git_clean() so the expensive
    bootstrap + VCS provider import can be deferred until after the
    child process has been forked.
    """
    from ...core.bootstrap import bootstrap
    from ...core.container import get_container

    bootstrap()
    vcs = get_container().get_vcs_provider("git")
    vcs_info = vcs.get_info(repo_root)
    return {
        "commit": vcs_info.commit if vcs_info else None,
        "branch": vcs_info.branch if vcs_info else None,
        "remote_url": vcs_info.remote_url if vcs_info else None,
    }


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
    step_name: str | None,
    quiet: bool,
    hash_algorithms: list[str],
    git_info: dict,
    repo_root: str,
    tracer_mode: str | None = None,
    tracer_fallback: bool | None = None,
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
        step_name: Optional user-defined step label
        quiet: Whether to suppress output
        hash_algorithms: List of hash algorithms to use
        git_info: Git info dict with commit, branch, remote_url
        repo_root: Git repository root path

    Returns:
        Exit code from the executed command
    """
    from typing import Literal, cast

    from ...config import config_get
    from ...core.interfaces.run import RunContext
    from ...services.execution import RunCoordinator

    # Check if S3 proxy is enabled
    proxy_service = None
    if config_get("proxy.enabled"):
        from ...services.execution.proxy import ProxyService

        proxy_service = ProxyService()
        if not proxy_service.find_proxy():
            click.echo(
                "Error: S3 proxy is enabled but roar-proxy binary not found.\n"
                "Build it with: cargo build --release --manifest-path rust/Cargo.toml -p roar-proxy\n"
                "Or disable: roar proxy disable",
                err=True,
            )
            return 1

    # Create run context
    hash_algos = cast(list[Literal["blake3", "sha256", "sha512", "md5"]], hash_algorithms)
    job_type_literal = cast(Literal["run", "build"] | None, job_type)
    run_ctx = RunContext(
        roar_dir=ctx.roar_dir,
        repo_root=repo_root,
        command=command,
        job_type=job_type_literal,
        step_name=step_name,
        quiet=quiet,
        hash_algorithms=hash_algos,
        tracer_mode=tracer_mode,  # type: ignore[arg-type]
        tracer_fallback=tracer_fallback,
        git_commit=git_info.get("commit"),
        git_branch=git_info.get("branch"),
        git_repo=git_info.get("remote_url"),
    )

    # Execute via coordinator
    coordinator = RunCoordinator(proxy_service=proxy_service)
    result = coordinator.execute(run_ctx)

    # Present report
    from ...presenters.console import ConsolePresenter
    from ...presenters.run_report import RunReportPresenter

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
