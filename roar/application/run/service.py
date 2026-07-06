"""Application orchestration for tracked run/build workflows."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ...db.context import create_database_context
from ...execution.framework.planning import plan_execution_command
from ...presenters.console import ConsolePresenter
from ...presenters.run_report import RunReportPresenter
from .dag_references import DAGReferenceResolver
from .execution import (
    ExecutionReport,
    execute_and_report,
    get_hash_algorithms,
    validate_git_clean,
)
from .requests import BuildRequest, RunRequest
from .verbosity import resolve_verbosity


@dataclass(frozen=True)
class _ExecutionInputs:
    command: list[str]
    job_type: str | None


@dataclass(frozen=True)
class _FinalizerContext:
    roar_dir: Path


def run_command(request: RunRequest) -> int:
    """Execute the `roar run` application workflow."""
    repo_root = validate_git_clean(verb="run", args=list(request.args), roar_dir=request.roar_dir)
    config_start_dir = _config_start_dir(request.roar_dir)
    verbosity = resolve_verbosity(
        cli_quiet=bool(request.quiet),
        cli_verbose=request.cli_verbose,
        repo_root=repo_root,
        config_start_dir=config_start_dir,
    )
    algorithms = get_hash_algorithms(
        list(request.hash_algorithms) if request.hash_algorithms else None,
        config_start_dir=config_start_dir,
    )
    execution_inputs = _resolve_run_inputs(request, verbosity=verbosity)
    if execution_inputs is None:
        return 0
    return _execute_tracked_command(
        roar_dir=request.roar_dir,
        config_start_dir=config_start_dir,
        repo_root=repo_root,
        command=execution_inputs.command,
        job_type=execution_inputs.job_type,
        step_name=request.step_name,
        verbosity=verbosity,
        hash_algorithms=algorithms,
        tracer_mode=request.tracer_mode,
        tracer_fallback=request.tracer_fallback,
        block_tags=list(request.block_tags),
        add_tags=list(request.add_tags),
    )


def build_command(request: BuildRequest) -> int:
    """Execute the `roar build` application workflow."""
    repo_root = validate_git_clean(verb="build", args=list(request.args), roar_dir=request.roar_dir)
    config_start_dir = _config_start_dir(request.roar_dir)
    verbosity = resolve_verbosity(
        cli_quiet=bool(request.quiet),
        cli_verbose=request.cli_verbose,
        repo_root=repo_root,
        config_start_dir=config_start_dir,
    )
    algorithms = get_hash_algorithms(
        list(request.hash_algorithms) if request.hash_algorithms else None,
        config_start_dir=config_start_dir,
    )
    command = list(request.args)
    if not command:
        raise ValueError("No command specified")
    return _execute_tracked_command(
        roar_dir=request.roar_dir,
        config_start_dir=config_start_dir,
        repo_root=repo_root,
        command=command,
        job_type="build",
        step_name=request.step_name,
        verbosity=verbosity,
        hash_algorithms=algorithms,
        tracer_mode=request.tracer_mode,
        tracer_fallback=request.tracer_fallback,
    )


def _execute_tracked_command(
    *,
    roar_dir: Path,
    config_start_dir: Path | None = None,
    repo_root: str,
    command: list[str],
    job_type: str | None,
    step_name: str | None,
    verbosity: str,
    hash_algorithms: list[str],
    tracer_mode: str | None,
    tracer_fallback: bool | None,
    block_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
) -> int:
    resolved_config_start_dir = config_start_dir or _config_start_dir(roar_dir)
    try:
        planned = plan_execution_command(command)
        backend_name = planned.backend_name
        execution_role = str(planned.execution_role or "").strip()
        if not execution_role:
            raise ValueError(
                f"Execution backend '{backend_name}' did not provide an execution role."
            )

        report = _coerce_execution_report(
            execute_and_report(
                roar_dir=roar_dir,
                backend_name=backend_name,
                execution_role=execution_role,
                command=planned.command,
                job_type=job_type,
                step_name=step_name,
                verbosity=verbosity,
                hash_algorithms=hash_algorithms,
                repo_root=repo_root,
                config_start_dir=resolved_config_start_dir,
                tracer_mode=tracer_mode,
                tracer_fallback=tracer_fallback,
                block_tags=block_tags,
                add_tags=add_tags,
            )
        )
    except Exception:
        _record_run_telemetry(
            job_type=job_type,
            report_exit_code=1,
            tracer_backend=None,
            failure_kind="internal",
            repo_root=repo_root,
        )
        raise

    try:
        if planned.finalize_run:
            planned.finalize_run(cast(Any, _FinalizerContext(roar_dir=roar_dir)))
    except Exception:
        _record_run_telemetry(
            job_type=job_type,
            report_exit_code=report.exit_code,
            tracer_backend=report.tracer_backend,
            failure_kind="internal",
            repo_root=repo_root,
        )
        raise

    _record_run_telemetry(
        job_type=job_type,
        report_exit_code=report.exit_code,
        tracer_backend=report.tracer_backend,
        failure_kind=_classify_run_failure(report),
        repo_root=repo_root,
    )

    return report.exit_code


def _config_start_dir(roar_dir: Path) -> Path:
    return roar_dir.parent


def _record_run_telemetry(
    *,
    job_type: str | None,
    report_exit_code: int,
    tracer_backend: str | None,
    failure_kind: str | None,
    repo_root: str,
) -> None:
    if job_type is not None:
        return

    from ...telemetry.hooks import record_run_outcome

    record_run_outcome(
        success=(report_exit_code == 0 and failure_kind is None),
        failure_kind=failure_kind,
        tracer_backend=tracer_backend,
        start_dir=repo_root,
    )


def _coerce_execution_report(value: ExecutionReport | int) -> ExecutionReport:
    if isinstance(value, ExecutionReport):
        return value
    return ExecutionReport(exit_code=int(value))


def _classify_run_failure(report: ExecutionReport) -> str | None:
    if report.exit_code == 0:
        return None
    if report.setup_error:
        return "tracer_setup"
    if report.interrupted:
        return "interrupted"
    return "user_exit"


def _resolve_run_inputs(
    request: RunRequest, *, verbosity: str = "normal"
) -> _ExecutionInputs | None:
    args_list = list(request.args)
    dag_reference: str | None = None
    param_overrides: dict[str, str] = {}
    command: list[str] = []

    i = 0
    while i < len(args_list):
        arg = args_list[i]
        if arg.startswith("@") and dag_reference is None:
            dag_reference = arg
            i += 1
            continue
        if dag_reference and arg.startswith("--") and "=" in arg:
            key, value = arg[2:].split("=", 1)
            param_overrides[key] = value
            i += 1
            continue
        command.append(arg)
        i += 1

    if dag_reference:
        return _resolve_dag_reference(
            roar_dir=request.roar_dir,
            reference=dag_reference,
            param_overrides=param_overrides,
            verbosity=verbosity,
        )

    if not command:
        raise ValueError("No command specified")
    return _ExecutionInputs(command=command, job_type=None)


def _resolve_dag_reference(
    *,
    roar_dir: Path,
    reference: str,
    param_overrides: dict[str, str],
    verbosity: str = "normal",
) -> _ExecutionInputs | None:
    with create_database_context(roar_dir) as db_ctx:
        resolver = DAGReferenceResolver(
            db_ctx.sessions,
            db_ctx.jobs,
            db_ctx.artifacts,
            db_ctx.lineage,
            db_ctx.session_service,
        )
        resolved, error = resolver.resolve(reference, param_overrides)

    if error:
        raise ValueError(error)
    if resolved is None:
        raise ValueError(f"Could not resolve DAG reference: {reference}")

    quiet = verbosity == "quiet"
    presenter = ConsolePresenter()
    report = RunReportPresenter(presenter, verbosity=cast(Any, verbosity))
    # In quiet mode we skip the interactive stale-upstream prompt and
    # the "Re-running @N" chrome — the user has asked for the wrapped
    # command's output to stand alone. They're trading safety signals
    # for silence; that's the explicit deal `-q` makes on `roar run`.
    if resolved.stale_upstream and not quiet:
        if not report.show_upstream_stale_warning(resolved.step_number, resolved.stale_upstream):
            presenter.print("Aborted.")
            return None
        presenter.print("")

    if not quiet:
        presenter.print(f"Re-running @{resolved.step_number}: {resolved.command}")
        presenter.print("")

    return _ExecutionInputs(
        command=shlex.split(resolved.command),
        job_type="build" if resolved.is_build else None,
    )
