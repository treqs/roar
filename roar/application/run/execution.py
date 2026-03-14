"""Shared execution helpers for tracked run/build application flows."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

import click

from ...core.models.run import RunContext
from ...execution.framework.registry import get_execution_backend
from ...execution.runtime.host_execution import ExecutionSetupError
from ...presenters.console import ConsolePresenter
from ...presenters.run_report import RunReportPresenter


def validate_git_clean() -> str:
    """Validate git repository is clean and return repo root."""
    import subprocess

    cwd = os.getcwd()

    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        from ...execution.framework.registry import is_execution_backend_job_environment

        if is_execution_backend_job_environment():
            return cwd
        raise ValueError(
            "roar requires the working directory to be inside a git repository."
        ) from None

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
        raise ValueError("\n".join(lines))

    return repo_root


def get_quiet_setting(quiet_flag: bool | None, repo_root: str | Path) -> bool:
    """Get quiet setting from CLI flag or config."""
    if quiet_flag is not None:
        return quiet_flag

    try:
        try:
            import tomllib as _tomllib
        except ImportError:
            import tomli as _tomllib  # type: ignore[no-redef]

        config_toml = Path(repo_root) / ".roar" / "config.toml" if repo_root else None
        if config_toml is not None and config_toml.exists():
            data = _tomllib.loads(config_toml.read_text())
            return bool(data.get("output", {}).get("quiet", False))
        return False
    except Exception:
        pass

    from ...integrations.config import load_config

    config = load_config(start_dir=str(repo_root) if repo_root else None)
    return config.get("output", {}).get("quiet", False)


def get_hash_algorithms(cli_algorithms: list[str] | None = None) -> list[str]:
    """Get hash algorithms from CLI or config."""
    from ...integrations.config import get_hash_algorithms as config_get_hash_algorithms

    return config_get_hash_algorithms(
        operation="run",
        cli_algorithms=cli_algorithms if cli_algorithms else None,
        hash_only=False,
    )


def execute_and_report(
    *,
    roar_dir: Path,
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
    """Execute command via selected backend and show the run report."""
    hash_algos = cast(list[Literal["blake3", "sha256", "sha512", "md5"]], hash_algorithms)
    job_type_literal = cast(Literal["run", "build"] | None, job_type)
    run_ctx = RunContext(
        roar_dir=roar_dir,
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

    presenter = ConsolePresenter()
    report = RunReportPresenter(presenter)
    report.show_report(result, command, quiet)

    if result.stale_upstream or result.stale_downstream:
        report.show_stale_warnings(
            result.stale_upstream,
            result.stale_downstream,
            is_build=(job_type == "build"),
        )

    return result.exit_code
