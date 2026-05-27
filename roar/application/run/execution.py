"""Shared execution helpers for tracked run/build application flows."""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import click

from ...core.models.run import RunContext
from ...execution.framework.registry import get_execution_backend
from ...execution.runtime.errors import ExecutionSetupError
from ...presenters.console import ConsolePresenter
from ...presenters.run_report import RunReportPresenter


@dataclass(frozen=True)
class ExecutionReport:
    """Minimal execution outcome used for post-finalizer telemetry."""

    exit_code: int
    tracer_backend: str | None = None
    interrupted: bool = False
    setup_error: bool = False


def validate_git_clean(
    *,
    verb: str = "run",
    args: list[str] | None = None,
    roar_dir: Path | str | None = None,
) -> str:
    """Validate git repository is clean and return repo root.

    On a dirty tree, raises ``ValueError`` with a teaching message: the
    principle behind the rule, the exact remediation commands using the
    user's own filenames and original CLI args, and a docs link. The
    ``verb``/``args`` parameters customize the recovery line so users see
    the exact ``roar run python …`` (or ``roar build …``) they typed.

    When ``roar_dir`` is provided and a roar DB is reachable, the dirty
    paths are classified into code / prior-roar outputs / unknown so the
    message recommends the right fix per bucket (commit vs .gitignore).
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
        # Preserve leading whitespace in porcelain output: the two-column
        # status code starts with a space for worktree-only modifications
        # (` M path`), and `.strip()` would eat that space and shift the
        # parser by one character — turning `train.py` into `rain.py`.
        status_output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        status_output = ""

    if status_output.strip():
        from .dirty_tree_error import format_dirty_tree_error

        # Build the message inside an ExitStack so the DB context (if we
        # opened one) is closed before we leave this scope, and we raise
        # the bare string-bearing exception outside any context manager.
        # An earlier version wrapped the raise inside a `@contextmanager`
        # generator with a too-broad `try/except`; the re-thrown
        # ValueError was caught and the generator tried to yield a
        # second time, surfacing as `generator didn't stop after throw()`.
        with ExitStack() as stack:
            artifact_lookup = _open_artifact_lookup(roar_dir, stack)
            message = format_dirty_tree_error(
                status_output=status_output,
                repo_root=repo_root,
                verb=verb,
                args=args,
                artifact_lookup=artifact_lookup,
            )
        raise ValueError(message)

    return repo_root


def _open_artifact_lookup(roar_dir: Path | str | None, stack: ExitStack) -> Any:
    """Open the roar DB if reachable and return ``db_ctx.artifacts``, else None.

    Registers the DB context with ``stack`` for cleanup. Failures here
    (no DB, corrupt DB, etc.) are silent — the classifier treats ``None``
    as "skip the prior-roar-output bucket" so fresh-init repos still
    get a sensible message.
    """
    if roar_dir is None:
        return None
    try:
        from ...db.query_context import create_query_database_context

        db_ctx = stack.enter_context(create_query_database_context(Path(roar_dir)))
    except Exception:
        return None
    return db_ctx.artifacts


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
) -> ExecutionReport:
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
        return ExecutionReport(exit_code=1, setup_error=True)

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
    report.next_steps_hint(result)
    report.tmp_filtered_hint(result)

    # Warn now if the run left the tree dirty so the *next* `roar run`
    # doesn't surprise the user with a refusal they could have fixed in
    # one .gitignore line right now.
    from .output_followup import emit_dirty_outputs_warning

    emit_dirty_outputs_warning(
        repo_root=repo_root,
        stream=report._stream,
        caps=report._caps,
        quiet=(verbosity == "quiet"),
    )

    if result.stale_upstream or result.stale_downstream:
        report.show_stale_warnings(
            result.stale_upstream,
            result.stale_downstream,
            is_build=(job_type == "build"),
        )

    setup_error = bool(result.exit_code and result.job_id == 0)
    return ExecutionReport(
        exit_code=result.exit_code,
        tracer_backend=result.backend,
        interrupted=result.interrupted,
        setup_error=setup_error,
    )
