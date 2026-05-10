"""Shared execution helpers for tracked run/build application flows."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

import click

from ...core.models.run import RunContext
from ...execution.framework.registry import get_execution_backend
from ...execution.runtime.errors import ExecutionSetupError
from ...presenters.console import ConsolePresenter
from ...presenters.run_report import RunReportPresenter


def validate_git_clean(*, verb: str = "run", args: list[str] | None = None) -> str:
    """Validate git repository is clean and return repo root.

    On a dirty tree, raises `ValueError` with a teaching message: the
    principle behind the rule, the exact remediation commands using the
    user's own filenames and original CLI args, and a docs link. The
    `verb`/`args` parameters customize the recovery line so users see
    the exact `roar run python …` (or `roar build …`) they typed.
    """
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
        from .dirty_tree_error import format_dirty_tree_error

        raise ValueError(
            format_dirty_tree_error(
                status_output=status_output,
                repo_root=repo_root,
                verb=verb,
                args=args,
            )
        )

    return repo_root


def get_quiet_setting(quiet_flag: bool | None, repo_root: str | Path) -> bool:
    """Get quiet setting from CLI flag or config (legacy helper).

    Prefer `resolve_verbosity()` from `verbosity.py` for new code; this
    helper remains for callers that haven't migrated yet.
    """
    from .verbosity import resolve_verbosity

    return (
        resolve_verbosity(
            cli_quiet=bool(quiet_flag),
            cli_verbose=0,
            repo_root=repo_root,
        )
        == "quiet"
    )


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
    verbosity: str = "normal",
    hash_algorithms: list[str],
    repo_root: str,
    tracer_mode: str | None = None,
    tracer_fallback: bool | None = None,
) -> int:
    """Execute command via selected backend and show the run report."""
    hash_algos = cast(list[Literal["blake3", "sha256", "sha512", "md5"]], hash_algorithms)
    job_type_literal = cast(Literal["run", "build"] | None, job_type)
    quiet = verbosity == "quiet"
    run_ctx = RunContext(
        roar_dir=roar_dir,
        repo_root=repo_root,
        command=command,
        execution_backend=backend_name,
        execution_role=execution_role,
        job_type=job_type_literal,
        step_name=step_name,
        quiet=quiet,
        verbosity=verbosity,  # type: ignore[arg-type]
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
    report = RunReportPresenter(presenter, verbosity=verbosity)  # type: ignore[arg-type]

    # The coordinator already emitted the lifecycle lines (trace start/end,
    # hashing spinner, lineage captured). Here we emit the summary block and
    # the final "done" line.
    report.summary(result, command)
    report.done(
        exit_code=result.exit_code,
        trace_duration=result.duration,
        post_duration=result.post_duration,
    )

    if result.stale_upstream or result.stale_downstream:
        report.show_stale_warnings(
            result.stale_upstream,
            result.stale_downstream,
            is_build=(job_type == "build"),
        )

    return result.exit_code
