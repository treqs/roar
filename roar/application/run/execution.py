"""Shared execution helpers for tracked run/build application flows."""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import click

from ...core.exceptions import CommandNotFoundError
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


RUN_REPORT_FILE_ENV = "ROAR_RUN_REPORT_FILE"


def write_run_report_file(report: ExecutionReport) -> None:
    """Write a machine-readable run outcome when ROAR_RUN_REPORT_FILE is set.

    Wrappers that cannot see past the exit code (the k8s pod entrypoint) use
    ``setup_error`` to distinguish "roar failed before launching the
    workload" from "the workload ran and failed" — only the former is safe
    to rerun uninstrumented. Advisory only: never fails the run.
    """
    target = str(os.environ.get(RUN_REPORT_FILE_ENV) or "").strip()
    if not target:
        return
    import contextlib
    import json

    payload = {
        "exit_code": report.exit_code,
        "setup_error": report.setup_error,
        "tracer_backend": report.tracer_backend,
    }
    with contextlib.suppress(OSError):
        Path(target).write_text(json.dumps(payload), encoding="utf-8")


def validate_git_clean(
    *,
    verb: str = "run",
    args: list[str] | None = None,
    roar_dir: Path | str | None = None,
) -> str:
    """Resolve the working root, enforcing a clean tree when in a git repo.

    Returns the repo root when inside a git repository, or the current
    working directory when not (roar runs outside a repo too — lineage is
    just captured without a commit; the run report nudges the user toward
    a repo via ``no_repo_hint``).

    On a dirty tree (in-repo only), raises ``ValueError`` with a teaching
    message: the principle behind the rule, the exact remediation commands
    using the user's own filenames and original CLI args, and a docs link.
    The ``verb``/``args`` parameters customize the recovery line so users
    see the exact ``roar run python …`` (or ``roar build …``) they typed.

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
        # Not inside a git repository (or git isn't installed). roar still
        # runs here: lineage is captured without a commit, and the run
        # report nudges the user to run inside a repo if they want
        # reproducible, registerable lineage (see RunReportPresenter
        # .no_repo_hint). The working directory stands in for repo_root so
        # downstream path logic keeps a non-empty root. There is no dirty
        # tree to validate when there is no tree, so we return early. This
        # also covers execution-backend job drivers (e.g. Ray) that run
        # from an extracted working_dir without a .git.
        return cwd

    # `roar reproduce` re-runs the recorded steps, which legitimately recreate
    # the run's outputs and so dirty the tree; enforcing the clean-tree rule on
    # the reproduction's own intermediates would deadlock it (step 1's output
    # blocks step 2). The reproduce executor sets ROAR_REPRODUCE for exactly
    # this window, so skip the dirty-tree gate here.
    if os.environ.get("ROAR_REPRODUCE") == "1":
        return repo_root

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


def get_quiet_setting(
    quiet_flag: bool | None,
    repo_root: str | Path,
    config_start_dir: str | Path | None = None,
) -> bool:
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
            config_start_dir=config_start_dir,
        )
        == "quiet"
    )


def get_hash_algorithms(
    cli_algorithms: list[str] | None = None,
    config_start_dir: str | Path | None = None,
) -> list[str]:
    """Get hash algorithms from CLI or config."""
    from ...integrations.config import get_hash_algorithms as config_get_hash_algorithms

    return config_get_hash_algorithms(
        operation="run",
        cli_algorithms=cli_algorithms if cli_algorithms else None,
        hash_only=False,
        start_dir=str(config_start_dir) if config_start_dir is not None else None,
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
    config_start_dir: str | Path | None = None,
    tracer_mode: str | None = None,
    tracer_fallback: bool | None = None,
    block_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
) -> ExecutionReport:
    """Execute command via selected backend and show the run report."""
    hash_algos = cast(list[Literal["blake3", "sha256", "sha512", "md5"]], hash_algorithms)
    job_type_literal = cast(Literal["run", "build"] | None, job_type)
    quiet = verbosity == "quiet"
    resolved_config_start_dir = Path(config_start_dir) if config_start_dir is not None else None
    run_ctx = RunContext(
        roar_dir=roar_dir,
        config_start_dir=resolved_config_start_dir,
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
        block_tags=block_tags or [],
        add_tags=add_tags or [],
    )

    backend = get_execution_backend(backend_name)
    try:
        result = backend.host_execution.execute(run_ctx)
    except CommandNotFoundError as exc:
        # Nothing ran: report it like a shell would (clean line, exit 127),
        # with no lineage job, done line, or register hints.
        ConsolePresenter().print_error(exc.message)
        return ExecutionReport(exit_code=exc.exit_code, setup_error=True)
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
    report.no_repo_hint(result)

    # Warn now if the run left the tree dirty so the *next* `roar run`
    # doesn't surprise the user with a refusal they could have fixed in
    # one .gitignore line right now.
    from .output_followup import emit_dirty_outputs_warning, emit_unsourced_inputs_nudge

    emit_dirty_outputs_warning(
        repo_root=repo_root,
        stream=report._stream,
        caps=report._caps,
        quiet=(verbosity == "quiet"),
    )

    # Nudge now if this run read inputs nothing tracked produced — the moment
    # the unsourced dependency is created is the cheapest time to hear about it.
    job_ref = f"@{result.step_number}" if result.step_number is not None else result.job_uid
    if job_ref:
        emit_unsourced_inputs_nudge(
            roar_dir=roar_dir,
            cwd=repo_root,
            job_ref=job_ref,
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
