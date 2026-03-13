"""
Shared execution helpers for run and build commands.

This module extracts common logic used by both `roar run` and `roar build`
commands, following the DRY principle. Both commands share ~70% of their
implementation: git validation, quiet setting resolution, execution
coordination, and result reporting.

Usage:
    from ._execution import validate_git_clean, get_quiet_setting, execute_and_report
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ..context import RoarContext


def validate_git_clean() -> str:
    """
    Validate git repository is clean and return repo root.

    Lightweight pre-fork check using git subprocesses directly, avoiding
    the heavy bootstrap/import cascade. Git info (commit, branch, etc.)
    is collected by the ProvenanceService after the fork.

    Returns:
        Repository root path

    Raises:
        click.ClickException: If not in a git repo or has uncommitted changes
    """
    import subprocess

    cwd = os.getcwd()

    # Find repo root (no bootstrap needed)
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        from ...execution.framework.registry import is_execution_backend_job_environment

        # Distributed backend jobs may run from extracted working dirs that are
        # not git repos. Allow execution there while preserving git checks
        # everywhere else.
        if is_execution_backend_job_environment():
            return cwd
        raise click.ClickException(
            "roar requires the working directory to be inside a git repository."
        ) from None

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

    return repo_root


def get_quiet_setting(quiet_flag: bool | None, repo_root: str | Path) -> bool:
    """
    Get quiet setting from CLI flag or config.

    The CLI flag takes precedence. If not provided, checks the config
    for `output.quiet` setting.

    Fast path: reads TOML directly — avoids loading Pydantic/settings for
    this single key.  Falls back to the full settings stack only if the
    direct read fails.

    Args:
        quiet_flag: Explicit quiet flag from command line (None if not specified)
        repo_root: Repository root for config lookup

    Returns:
        Whether to use quiet mode
    """
    if quiet_flag is not None:
        return quiet_flag

    # Fast path: read config TOML directly without loading Pydantic.
    # This is the hot path — called before the child process starts.
    from pathlib import Path as _Path

    try:
        try:
            import tomllib as _tomllib
        except ImportError:
            import tomli as _tomllib  # type: ignore[no-redef]

        config_toml = _Path(repo_root) / ".roar" / "config.toml" if repo_root else None
        if config_toml is not None and config_toml.exists():
            data = _tomllib.loads(config_toml.read_text())
            return bool(data.get("output", {}).get("quiet", False))
        # No config file found — use default.
        return False
    except Exception:
        pass

    # Fallback: full settings stack (covers edge cases like custom config paths).
    from ...config import load_config

    config = load_config(start_dir=str(repo_root) if repo_root else None)
    return config.get("output", {}).get("quiet", False)


def execute_and_report(
    ctx: "RoarContext",
    backend_name: str,
    execution_role: str,
    command: list[str],
    job_type: str | None,
    step_name: str | None,
    quiet: bool,
    hash_algorithms: list[str],
    repo_root: str,
    tracer_mode: str | None = None,
    tracer_fallback: bool | None = None,
) -> int:
    """
    Execute command via coordinator and show report.

    This is the core execution function shared between run and build.
    It handles:
    1. Creating the RunContext
    2. Dispatching through the selected execution backend
    3. Showing the result report
    4. Displaying stale step warnings

    Args:
        ctx: RoarContext with roar_dir and other context
        command: Command to execute as list of strings
        job_type: Job type - None for run, "build" for build
        step_name: Optional user-defined step label
        quiet: Whether to suppress output
        hash_algorithms: List of hash algorithms to use
        repo_root: Git repository root path

    Returns:
        Exit code from the executed command
    """
    from typing import Literal, cast

    from ...core.interfaces.run import RunContext
    from ...execution.framework.registry import get_execution_backend
    from ...execution.runtime.host_execution import ExecutionSetupError

    # Create run context
    hash_algos = cast(list[Literal["blake3", "sha256", "sha512", "md5"]], hash_algorithms)
    job_type_literal = cast(Literal["run", "build"] | None, job_type)
    run_ctx = RunContext(
        roar_dir=ctx.roar_dir,
        repo_root=repo_root,
        command=command,
        execution_backend=backend_name,
        execution_role=execution_role,
        job_type=job_type_literal,
        step_name=step_name,
        quiet=quiet,
        hash_algorithms=hash_algos,
        tracer_mode=tracer_mode,  # type: ignore[arg-type]
        tracer_fallback=tracer_fallback,
    )

    backend = get_execution_backend(backend_name)
    try:
        result = backend.host_execution.execute(run_ctx)
    except ExecutionSetupError as exc:
        click.echo(str(exc), err=True)
        return 1

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
