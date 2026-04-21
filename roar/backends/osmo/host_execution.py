from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from roar.application.system_labels import refresh_job_system_labels
from roar.backends.osmo.config import load_osmo_backend_config
from roar.backends.osmo.lineage import (
    OsmoLineageReconstitutionResult,
    discover_downloaded_lineage_bundles,
    reconstitute_osmo_lineage_bundles,
)
from roar.backends.osmo.workflow import (
    prepare_osmo_workflow_for_lineage,
    resolve_roar_install_requirement,
)
from roar.core.bootstrap import bootstrap
from roar.core.models.run import RunContext, RunResult
from roar.core.operation_metadata import build_operation_metadata_json
from roar.db.context import create_database_context
from roar.db.hashing import hash_files_blake3
from roar.execution.recording import LocalJobRecorder, LocalRecordedArtifact, StalenessAnalyzer
from roar.execution.runtime.errors import ExecutionSetupError

_TERMINAL_WORKFLOW_STATUSES = {
    "CANCELLED",
    "CANCELED",
    "COMPLETED",
    "FAILED",
    "TERMINATED",
    "TIMED_OUT",
    "TIMEOUT",
}
_OSMO_TEMPLATE_SENTINEL_RE = re.compile(r"__ROAR_OSMO_TEMPLATE_([A-Za-z0-9_.-]+)__")


@dataclass(frozen=True)
class OsmoWorkflowWaitResult:
    status: str | None = None
    payload: dict[str, Any] | None = None
    timed_out: bool = False
    error: str | None = None


@dataclass(frozen=True)
class OsmoSubmitCommandContext:
    repo_root: str | None = None
    workflow_spec_argument: str | None = None
    workflow_spec_path: str | None = None
    prepared_workflow_argument: str | None = None
    prepared_workflow_path: str | None = None
    prepared_wrapped_tasks: list[str] | None = None
    prepared_runtime_install_requirement: str | None = None
    prepared_runtime_install_local_path: str | None = None
    prepared_runtime_install_remote_path: str | None = None
    pool: str | None = None
    format_type: str | None = None
    set_strings: dict[str, str] | None = None
    set_files: dict[str, str] | None = None
    dataset_hints: list[str] | None = None
    task_name_hints: list[str] | None = None


@dataclass(frozen=True)
class OsmoDeclaredDatasetOutput:
    dataset_name: str
    declared_path: str | None = None
    task_name: str | None = None


@dataclass(frozen=True)
class OsmoOutputDownloadResult:
    artifacts: list[LocalRecordedArtifact] = field(default_factory=list)
    datasets: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class OsmoWorkflowDiagnosticsResult:
    artifacts: list[LocalRecordedArtifact] = field(default_factory=list)
    query_artifact_path: str | None = None
    task_logs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class OsmoAttachOptions:
    workflow_spec_argument: str | None = None
    workflow_spec_path: str | None = None
    set_strings: dict[str, str] | None = None
    dataset_names: list[str] | None = None
    task_names: list[str] | None = None
    wait_for_completion: bool | None = None
    download_declared_outputs: bool | None = None
    ingest_lineage_bundles: bool | None = None


def execute_osmo_workflow_submit(ctx: RunContext) -> RunResult:
    """Execute an OSMO workflow submit locally and record it as a Roar job."""
    bootstrap(ctx.roar_dir)
    started_at = time.time()
    config = load_osmo_backend_config(start_dir=ctx.repo_root)
    submit_context = _extract_submit_command_context(ctx.command, ctx.repo_root)
    submit_context = _merge_configured_osmo_context_hints(
        submit_context,
        config=config,
        include_lineage_dataset_hint=bool(config.get("ingest_lineage_bundles", False)),
    )
    submit_command, submit_context, prepared_workflow_path = _prepare_submit_command(
        command=ctx.command,
        submit_context=submit_context,
        config=config,
    )

    try:
        try:
            completed = subprocess.run(
                submit_command,
                cwd=ctx.repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            if prepared_workflow_path is not None:
                prepared_workflow_path.unlink(missing_ok=True)
    except FileNotFoundError as exc:
        raise ExecutionSetupError(
            "Error: osmo CLI not found. Install the OSMO CLI or adjust PATH."
        ) from exc

    _emit_captured_output(completed.stdout, sys.stdout)
    _emit_captured_output(completed.stderr, sys.stderr)

    workflow_id = _extract_workflow_id(_parse_json_response(completed.stdout))
    wait_result: OsmoWorkflowWaitResult | None = None
    final_exit_code = completed.returncode
    if completed.returncode == 0 and bool(config.get("wait_for_completion", False)):
        wait_result = _wait_for_workflow_completion(
            command=ctx.command,
            repo_root=ctx.repo_root,
            workflow_id=workflow_id,
            timeout_seconds=int(config.get("query_timeout_seconds", 12 * 60)),
            poll_interval_seconds=float(config.get("poll_interval_seconds", 5.0)),
        )
        final_exit_code = _resolve_final_exit_code(completed.returncode, wait_result)

    download_result: OsmoOutputDownloadResult | None = None
    if bool(config.get("download_declared_outputs", False)):
        download_result = _download_declared_outputs(
            osmo_binary=ctx.command[0],
            repo_root=ctx.repo_root,
            roar_dir=ctx.roar_dir,
            submit_context=submit_context,
            workflow_id=workflow_id,
            wait_result=wait_result,
            download_directory=str(config.get("download_directory", ".roar/osmo/downloads")),
        )
        if download_result.error and final_exit_code == 0:
            final_exit_code = 1

    diagnostics_result = _capture_workflow_diagnostics(
        osmo_binary=ctx.command[0],
        repo_root=ctx.repo_root,
        roar_dir=ctx.roar_dir,
        submit_context=submit_context,
        workflow_id=workflow_id,
        wait_result=wait_result,
    )

    duration = max(0.0, time.time() - started_at)
    input_artifacts = _build_submit_input_artifacts(submit_context)
    initial_payload = _build_osmo_submit_payload(
        ctx=ctx,
        submit_context=submit_context,
        started_at=started_at,
        duration=duration,
        completed=completed,
        final_exit_code=final_exit_code,
        wait_enabled=bool(config.get("wait_for_completion", False)),
        wait_result=wait_result,
        download_result=download_result,
        diagnostics_result=diagnostics_result,
        lineage_result=None,
    )
    metadata = build_operation_metadata_json("osmo_submit", initial_payload)
    non_receipt_output_artifacts = [
        *(download_result.artifacts if download_result is not None else []),
        *diagnostics_result.artifacts,
    ]

    with create_database_context(ctx.roar_dir) as db_ctx:
        session_id = db_ctx.sessions.get_or_create_active()
        recorder = LocalJobRecorder()
        job_id, job_uid = recorder.record(
            db_ctx,
            command=shlex.join(ctx.command),
            timestamp=started_at,
            metadata=metadata,
            execution_backend=ctx.execution_backend,
            execution_role=ctx.execution_role,
            job_type=ctx.job_type or "run",
            input_artifacts=input_artifacts,
            output_artifacts=non_receipt_output_artifacts,
            duration_seconds=duration,
            exit_code=final_exit_code,
            session_id=session_id,
        )
        job = db_ctx.jobs.get(job_id)
        resolved_session_id = (
            int(job["session_id"]) if job and job.get("session_id") else session_id
        )
        submit_step_number = int(job["step_number"]) if job and job.get("step_number") else 1
        db_ctx.commit()

    lineage_result = _maybe_reconstitute_downloaded_lineage(
        config=config,
        download_result=download_result,
        repo_root=ctx.repo_root,
        roar_dir=ctx.roar_dir,
        job_uid=job_uid,
        session_id=resolved_session_id,
        submit_step_number=submit_step_number,
    )

    final_payload = _build_osmo_submit_payload(
        ctx=ctx,
        submit_context=submit_context,
        started_at=started_at,
        duration=duration,
        completed=completed,
        final_exit_code=final_exit_code,
        wait_enabled=bool(config.get("wait_for_completion", False)),
        wait_result=wait_result,
        download_result=download_result,
        diagnostics_result=diagnostics_result,
        lineage_result=lineage_result,
    )
    receipt_artifact = _write_osmo_submit_receipt(
        roar_dir=ctx.roar_dir,
        payload=final_payload,
    )

    with create_database_context(ctx.roar_dir) as db_ctx:
        _update_recorded_osmo_submit(
            db_ctx=db_ctx,
            job_id=job_id,
            metadata=build_operation_metadata_json("osmo_submit", final_payload),
            receipt_artifact=receipt_artifact,
        )
        stale_upstream, stale_downstream = StalenessAnalyzer().analyze(
            db_ctx, resolved_session_id, job_id
        )
        inputs = db_ctx.jobs.get_inputs(job_id)
        outputs = db_ctx.jobs.get_outputs(job_id)

    return RunResult(
        exit_code=final_exit_code,
        job_id=job_id,
        job_uid=job_uid,
        duration=duration,
        inputs=inputs,
        outputs=outputs,
        interrupted=False,
        is_build=ctx.job_type == "build",
        stale_upstream=stale_upstream,
        stale_downstream=stale_downstream,
    )


def attach_osmo_workflow(
    *,
    roar_dir: Path,
    repo_root: str,
    workflow_id: str,
    options: OsmoAttachOptions | None = None,
    osmo_binary: str = "osmo",
) -> RunResult:
    """Attach local Roar lineage to an existing OSMO workflow."""
    bootstrap(roar_dir)
    started_at = time.time()
    config = load_osmo_backend_config(start_dir=repo_root)
    effective_options = options or OsmoAttachOptions()
    attach_context = _build_attach_context(
        repo_root=repo_root,
        workflow_spec_argument=effective_options.workflow_spec_argument,
        workflow_spec_path=effective_options.workflow_spec_path,
        set_strings=effective_options.set_strings,
        dataset_names=effective_options.dataset_names,
        task_names=effective_options.task_names,
    )
    wait_enabled = (
        bool(config.get("wait_for_completion", False))
        if effective_options.wait_for_completion is None
        else bool(effective_options.wait_for_completion)
    )
    download_enabled = (
        bool(config.get("download_declared_outputs", False))
        if effective_options.download_declared_outputs is None
        else bool(effective_options.download_declared_outputs)
    )
    ingest_lineage_enabled = (
        bool(config.get("ingest_lineage_bundles", False))
        if effective_options.ingest_lineage_bundles is None
        else bool(effective_options.ingest_lineage_bundles)
    )
    attach_context = _merge_configured_osmo_context_hints(
        attach_context,
        config=config,
        include_lineage_dataset_hint=ingest_lineage_enabled,
    )

    wait_result = (
        _wait_for_workflow_completion(
            command=[osmo_binary],
            repo_root=repo_root,
            workflow_id=workflow_id,
            timeout_seconds=int(config.get("query_timeout_seconds", 12 * 60)),
            poll_interval_seconds=float(config.get("poll_interval_seconds", 5.0)),
        )
        if wait_enabled
        else _query_workflow_state(
            osmo_binary=osmo_binary,
            repo_root=repo_root,
            workflow_id=workflow_id,
        )
    )
    final_exit_code = _resolve_attach_exit_code(wait_result)

    download_result: OsmoOutputDownloadResult | None = None
    if download_enabled:
        download_result = _download_declared_outputs(
            osmo_binary=osmo_binary,
            repo_root=repo_root,
            roar_dir=roar_dir,
            submit_context=attach_context,
            workflow_id=workflow_id,
            wait_result=wait_result,
            download_directory=str(config.get("download_directory", ".roar/osmo/downloads")),
        )
        if download_result.error and final_exit_code == 0:
            final_exit_code = 1

    diagnostics_result = _capture_workflow_diagnostics(
        osmo_binary=osmo_binary,
        repo_root=repo_root,
        roar_dir=roar_dir,
        submit_context=attach_context,
        workflow_id=workflow_id,
        wait_result=wait_result,
    )

    duration = max(0.0, time.time() - started_at)
    input_artifacts = _build_osmo_input_artifacts(
        attach_context,
        metadata_key="osmo_attach_input",
    )
    initial_payload = _build_osmo_attach_payload(
        osmo_binary=osmo_binary,
        workflow_id=workflow_id,
        attach_context=attach_context,
        started_at=started_at,
        duration=duration,
        final_exit_code=final_exit_code,
        wait_enabled=wait_enabled,
        wait_result=wait_result,
        download_enabled=download_enabled,
        download_result=download_result,
        diagnostics_result=diagnostics_result,
        lineage_result=None,
        git_commit=_resolve_git_commit(repo_root),
    )
    metadata = build_operation_metadata_json("osmo_attach", initial_payload)
    non_receipt_output_artifacts = [
        *(download_result.artifacts if download_result is not None else []),
        *diagnostics_result.artifacts,
    ]
    command = [osmo_binary, "workflow", "attach", workflow_id]

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.get_or_create_active()
        recorder = LocalJobRecorder()
        job_id, job_uid = recorder.record(
            db_ctx,
            command=shlex.join(command),
            timestamp=started_at,
            metadata=metadata,
            execution_backend="osmo",
            execution_role="attach",
            job_type="run",
            input_artifacts=input_artifacts,
            output_artifacts=non_receipt_output_artifacts,
            duration_seconds=duration,
            exit_code=final_exit_code,
            session_id=session_id,
        )
        job = db_ctx.jobs.get(job_id)
        resolved_session_id = (
            int(job["session_id"]) if job and job.get("session_id") else session_id
        )
        attach_step_number = int(job["step_number"]) if job and job.get("step_number") else 1
        db_ctx.commit()

    lineage_result = _maybe_reconstitute_downloaded_lineage(
        config={**config, "ingest_lineage_bundles": ingest_lineage_enabled},
        download_result=download_result,
        repo_root=repo_root,
        roar_dir=roar_dir,
        job_uid=job_uid,
        session_id=resolved_session_id,
        submit_step_number=attach_step_number,
    )
    if lineage_result is not None and lineage_result.error and final_exit_code == 0:
        final_exit_code = 1

    final_payload = _build_osmo_attach_payload(
        osmo_binary=osmo_binary,
        workflow_id=workflow_id,
        attach_context=attach_context,
        started_at=started_at,
        duration=duration,
        final_exit_code=final_exit_code,
        wait_enabled=wait_enabled,
        wait_result=wait_result,
        download_enabled=download_enabled,
        download_result=download_result,
        diagnostics_result=diagnostics_result,
        lineage_result=lineage_result,
        git_commit=_resolve_git_commit(repo_root),
    )
    receipt_artifact = _write_osmo_attach_receipt(
        roar_dir=roar_dir,
        payload=final_payload,
    )

    with create_database_context(roar_dir) as db_ctx:
        _update_recorded_osmo_submit(
            db_ctx=db_ctx,
            job_id=job_id,
            metadata=build_operation_metadata_json("osmo_attach", final_payload),
            receipt_artifact=receipt_artifact,
        )
        stale_upstream, stale_downstream = StalenessAnalyzer().analyze(
            db_ctx, resolved_session_id, job_id
        )
        inputs = db_ctx.jobs.get_inputs(job_id)
        outputs = db_ctx.jobs.get_outputs(job_id)

    return RunResult(
        exit_code=final_exit_code,
        job_id=job_id,
        job_uid=job_uid,
        duration=duration,
        inputs=inputs,
        outputs=outputs,
        interrupted=False,
        is_build=False,
        stale_upstream=stale_upstream,
        stale_downstream=stale_downstream,
    )


def _emit_captured_output(text: str, stream: Any) -> None:
    if not text:
        return
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _build_osmo_submit_payload(
    *,
    ctx: RunContext,
    submit_context: OsmoSubmitCommandContext,
    started_at: float,
    duration: float,
    completed: subprocess.CompletedProcess[str],
    final_exit_code: int,
    wait_enabled: bool,
    wait_result: OsmoWorkflowWaitResult | None,
    download_result: OsmoOutputDownloadResult | None,
    diagnostics_result: OsmoWorkflowDiagnosticsResult,
    lineage_result: OsmoLineageReconstitutionResult | None,
) -> dict[str, Any]:
    parsed_response = _parse_json_response(completed.stdout)
    workflow_id = _extract_workflow_id(parsed_response)

    payload: dict[str, Any] = {
        "command": list(ctx.command),
        "command_string": shlex.join(ctx.command),
        "workflow_id": workflow_id,
        "response_format": "json" if parsed_response is not None else "text",
        "return_code": final_exit_code,
        "submit_return_code": completed.returncode,
        "duration_seconds": duration,
        "timestamp": started_at,
        "git_commit": _resolve_git_commit(ctx.repo_root),
        "wait_for_completion": wait_enabled,
    }
    submit_payload = _build_submit_context_payload(submit_context)
    if submit_payload:
        payload["submit"] = submit_payload
    if parsed_response is not None:
        payload["response"] = parsed_response
    else:
        payload["stdout"] = _truncate_output(completed.stdout)
    if completed.stderr.strip():
        payload["stderr"] = _truncate_output(completed.stderr)
    if wait_result is not None:
        payload["workflow_status"] = wait_result.status
        if wait_result.payload is not None:
            payload["workflow_query"] = wait_result.payload
        if wait_result.timed_out:
            payload["workflow_query_timed_out"] = True
        if wait_result.error:
            payload["workflow_query_error"] = wait_result.error
    if download_result is not None:
        payload["download_declared_outputs"] = True
        if download_result.datasets:
            payload["downloaded_outputs"] = list(download_result.datasets)
        if download_result.error:
            payload["download_error"] = download_result.error
    if (
        diagnostics_result.query_artifact_path
        or diagnostics_result.task_logs
        or diagnostics_result.error
    ):
        diagnostics_payload: dict[str, Any] = {}
        if diagnostics_result.query_artifact_path:
            diagnostics_payload["query_artifact_path"] = diagnostics_result.query_artifact_path
        if diagnostics_result.task_logs:
            diagnostics_payload["task_logs"] = list(diagnostics_result.task_logs)
        if diagnostics_result.error:
            diagnostics_payload["error"] = diagnostics_result.error
        payload["workflow_diagnostics"] = diagnostics_payload
    if lineage_result is not None:
        lineage_payload: dict[str, Any] = {
            "bundle_count": len(lineage_result.bundles),
            "fragments_processed": lineage_result.fragments_processed,
            "jobs_merged": lineage_result.jobs_merged,
            "artifacts_merged": lineage_result.artifacts_merged,
        }
        if lineage_result.bundles:
            lineage_payload["bundles"] = list(lineage_result.bundles)
        if lineage_result.error:
            lineage_payload["error"] = lineage_result.error
        payload["lineage_reconstitution"] = lineage_payload

    return payload


def _build_osmo_attach_payload(
    *,
    osmo_binary: str,
    workflow_id: str,
    attach_context: OsmoSubmitCommandContext,
    started_at: float,
    duration: float,
    final_exit_code: int,
    wait_enabled: bool,
    wait_result: OsmoWorkflowWaitResult | None,
    download_enabled: bool,
    download_result: OsmoOutputDownloadResult | None,
    diagnostics_result: OsmoWorkflowDiagnosticsResult,
    lineage_result: OsmoLineageReconstitutionResult | None,
    git_commit: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "command": [osmo_binary, "workflow", "attach", workflow_id],
        "command_string": shlex.join([osmo_binary, "workflow", "attach", workflow_id]),
        "workflow_id": workflow_id,
        "return_code": final_exit_code,
        "duration_seconds": duration,
        "timestamp": started_at,
        "git_commit": git_commit,
        "wait_for_completion": wait_enabled,
    }
    attach_payload = _build_submit_context_payload(attach_context)
    if attach_payload:
        payload["attach"] = attach_payload
    if wait_result is not None:
        payload["workflow_status"] = wait_result.status
        if wait_result.payload is not None:
            payload["workflow_query"] = wait_result.payload
        if wait_result.timed_out:
            payload["workflow_query_timed_out"] = True
        if wait_result.error:
            payload["workflow_query_error"] = wait_result.error
    if download_enabled:
        payload["download_declared_outputs"] = True
    if download_result is not None:
        if download_result.datasets:
            payload["downloaded_outputs"] = list(download_result.datasets)
        if download_result.error:
            payload["download_error"] = download_result.error
    if (
        diagnostics_result.query_artifact_path
        or diagnostics_result.task_logs
        or diagnostics_result.error
    ):
        diagnostics_payload: dict[str, Any] = {}
        if diagnostics_result.query_artifact_path:
            diagnostics_payload["query_artifact_path"] = diagnostics_result.query_artifact_path
        if diagnostics_result.task_logs:
            diagnostics_payload["task_logs"] = list(diagnostics_result.task_logs)
        if diagnostics_result.error:
            diagnostics_payload["error"] = diagnostics_result.error
        payload["workflow_diagnostics"] = diagnostics_payload
    if lineage_result is not None:
        lineage_payload: dict[str, Any] = {
            "bundle_count": len(lineage_result.bundles),
            "fragments_processed": lineage_result.fragments_processed,
            "jobs_merged": lineage_result.jobs_merged,
            "artifacts_merged": lineage_result.artifacts_merged,
        }
        if lineage_result.bundles:
            lineage_payload["bundles"] = list(lineage_result.bundles)
        if lineage_result.error:
            lineage_payload["error"] = lineage_result.error
        payload["lineage_reconstitution"] = lineage_payload
    return payload


def _parse_json_response(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_submit_command_context(command: list[str], repo_root: str) -> OsmoSubmitCommandContext:
    workflow_spec_argument = None
    workflow_spec_path = None
    if len(command) >= 4 and not command[3].startswith("-"):
        workflow_spec_argument = command[3]
        workflow_spec_path = _resolve_local_path(workflow_spec_argument, repo_root)

    pool = None
    format_type = None
    set_strings: dict[str, str] = {}
    set_files: dict[str, str] = {}

    i = 4
    while i < len(command):
        arg = command[i]
        if arg == "--pool" and i + 1 < len(command):
            pool = command[i + 1]
            i += 2
            continue
        if arg.startswith("--pool="):
            pool = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--format-type" and i + 1 < len(command):
            format_type = command[i + 1]
            i += 2
            continue
        if arg.startswith("--format-type="):
            format_type = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--set-string" and i + 1 < len(command):
            _assign_submit_mapping(set_strings, command[i + 1])
            i += 2
            continue
        if arg.startswith("--set-string="):
            _assign_submit_mapping(set_strings, arg.split("=", 1)[1])
            i += 1
            continue
        if arg == "--set-file" and i + 1 < len(command):
            _assign_submit_mapping(set_files, command[i + 1])
            i += 2
            continue
        if arg.startswith("--set-file="):
            _assign_submit_mapping(set_files, arg.split("=", 1)[1])
            i += 1
            continue
        i += 1

    return OsmoSubmitCommandContext(
        repo_root=repo_root,
        workflow_spec_argument=workflow_spec_argument,
        workflow_spec_path=workflow_spec_path,
        pool=pool,
        format_type=format_type,
        set_strings=set_strings or None,
        set_files=set_files or None,
    )


def _assign_submit_mapping(target: dict[str, str], value: str) -> None:
    if "=" not in value:
        return
    key, mapped_value = value.split("=", 1)
    key = key.strip()
    mapped_value = mapped_value.strip()
    if key and mapped_value:
        target[key] = mapped_value


def _build_attach_context(
    *,
    repo_root: str,
    workflow_spec_argument: str | None,
    workflow_spec_path: str | None,
    set_strings: dict[str, str] | None,
    dataset_names: list[str] | None = None,
    task_names: list[str] | None = None,
) -> OsmoSubmitCommandContext:
    resolved_spec_path = workflow_spec_path
    if resolved_spec_path:
        resolved_spec_path = _resolve_local_path(resolved_spec_path, repo_root)
    return OsmoSubmitCommandContext(
        repo_root=repo_root,
        workflow_spec_argument=workflow_spec_argument,
        workflow_spec_path=resolved_spec_path,
        set_strings=dict(set_strings) if set_strings else None,
        dataset_hints=[item for item in (dataset_names or []) if str(item).strip()] or None,
        task_name_hints=[item for item in (task_names or []) if str(item).strip()] or None,
    )


def _merge_configured_osmo_context_hints(
    context: OsmoSubmitCommandContext,
    *,
    config: dict[str, Any],
    include_lineage_dataset_hint: bool,
) -> OsmoSubmitCommandContext:
    dataset_hints = [
        str(item).strip() for item in (context.dataset_hints or []) if str(item).strip()
    ]
    if include_lineage_dataset_hint:
        lineage_dataset_name = str(config.get("lineage_bundle_dataset_name", "")).strip()
        if lineage_dataset_name and lineage_dataset_name not in dataset_hints:
            dataset_hints.append(lineage_dataset_name)

    if dataset_hints == list(context.dataset_hints or []):
        return context

    return OsmoSubmitCommandContext(
        repo_root=context.repo_root,
        workflow_spec_argument=context.workflow_spec_argument,
        workflow_spec_path=context.workflow_spec_path,
        prepared_workflow_argument=context.prepared_workflow_argument,
        prepared_workflow_path=context.prepared_workflow_path,
        prepared_wrapped_tasks=list(context.prepared_wrapped_tasks)
        if context.prepared_wrapped_tasks
        else None,
        prepared_runtime_install_requirement=context.prepared_runtime_install_requirement,
        prepared_runtime_install_local_path=context.prepared_runtime_install_local_path,
        prepared_runtime_install_remote_path=context.prepared_runtime_install_remote_path,
        pool=context.pool,
        format_type=context.format_type,
        set_strings=dict(context.set_strings) if context.set_strings else None,
        set_files=dict(context.set_files) if context.set_files else None,
        dataset_hints=dataset_hints or None,
        task_name_hints=list(context.task_name_hints) if context.task_name_hints else None,
    )


def _build_submit_context_payload(context: OsmoSubmitCommandContext) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if context.workflow_spec_argument or context.workflow_spec_path:
        payload["workflow_spec"] = {
            "argument": context.workflow_spec_argument,
            "path": context.workflow_spec_path,
        }
    if context.prepared_workflow_argument or context.prepared_workflow_path:
        prepared_payload: dict[str, Any] = {}
        if context.prepared_workflow_argument or context.prepared_workflow_path:
            prepared_payload["workflow_spec"] = {
                "argument": context.prepared_workflow_argument,
                "path": context.prepared_workflow_path,
            }
        if context.prepared_wrapped_tasks:
            prepared_payload["wrapped_tasks"] = list(context.prepared_wrapped_tasks)
        if context.prepared_runtime_install_requirement:
            prepared_payload["runtime_install_requirement"] = (
                context.prepared_runtime_install_requirement
            )
        if context.prepared_runtime_install_local_path:
            prepared_payload["runtime_install_local_path"] = (
                context.prepared_runtime_install_local_path
            )
        if context.prepared_runtime_install_remote_path:
            prepared_payload["runtime_install_remote_path"] = (
                context.prepared_runtime_install_remote_path
            )
        payload["prepared_workflow"] = prepared_payload
    if context.pool:
        payload["pool"] = context.pool
    if context.format_type:
        payload["format_type"] = context.format_type
    if context.set_strings:
        payload["set_strings"] = dict(context.set_strings)
    if context.set_files:
        payload["set_files"] = dict(context.set_files)
    if context.dataset_hints:
        payload["dataset_hints"] = list(context.dataset_hints)
    if context.task_name_hints:
        payload["task_name_hints"] = list(context.task_name_hints)
    return payload


def _prepare_submit_command(
    *,
    command: list[str],
    submit_context: OsmoSubmitCommandContext,
    config: dict[str, Any],
) -> tuple[list[str], OsmoSubmitCommandContext, Path | None]:
    if not bool(config.get("auto_prepare_submissions", True)):
        return list(command), submit_context, None
    if not submit_context.workflow_spec_path:
        return list(command), submit_context, None

    workflow_spec_path = Path(submit_context.workflow_spec_path)
    if not workflow_spec_path.is_file():
        return list(command), submit_context, None

    temp_output_path = _create_prepared_workflow_temp_path(workflow_spec_path)
    runtime_install_local_path = _resolve_local_path(
        str(config.get("runtime_install_local_path") or ""),
        submit_context.repo_root,
    )
    runtime_install_remote_path = (
        str(config.get("runtime_install_remote_path", "/tmp/roar-osmo-install.whl")).strip()
        or "/tmp/roar-osmo-install.whl"
    )
    runtime_install_requirement = (
        None
        if runtime_install_local_path
        else resolve_roar_install_requirement(
            _coerce_optional_text(config.get("runtime_install_requirement"))
        )
    )

    try:
        prepared = prepare_osmo_workflow_for_lineage(
            input_path=workflow_spec_path,
            output_path=temp_output_path,
            lineage_dataset_name=str(
                config.get("lineage_bundle_dataset_name", "roar-lineage")
            ).strip()
            or "roar-lineage",
            lineage_bundle_filename=str(
                config.get("lineage_bundle_filename", "roar-fragments.json")
            ).strip()
            or "roar-fragments.json",
            inject_runtime_wrapper=True,
            runtime_install_requirement=runtime_install_requirement,
            runtime_install_local_path=runtime_install_local_path,
            runtime_install_remote_path=runtime_install_remote_path,
            task_names=None,
            default_to_all_tasks=True,
        )
    except ValueError as exc:
        temp_output_path.unlink(missing_ok=True)
        raise ExecutionSetupError(
            f"Error preparing OSMO workflow for Roar instrumentation: {exc}"
        ) from exc

    rewritten_command = list(command)
    rewritten_argument = str(temp_output_path)
    if len(rewritten_command) >= 4:
        rewritten_command[3] = rewritten_argument

    rewritten_context = OsmoSubmitCommandContext(
        repo_root=submit_context.repo_root,
        workflow_spec_argument=submit_context.workflow_spec_argument,
        workflow_spec_path=submit_context.workflow_spec_path,
        prepared_workflow_argument=rewritten_argument,
        prepared_workflow_path=str(temp_output_path),
        prepared_wrapped_tasks=list(prepared.wrapped_tasks),
        prepared_runtime_install_requirement=runtime_install_requirement,
        prepared_runtime_install_local_path=runtime_install_local_path,
        prepared_runtime_install_remote_path=(
            runtime_install_remote_path if runtime_install_local_path else None
        ),
        pool=submit_context.pool,
        format_type=submit_context.format_type,
        set_strings=dict(submit_context.set_strings) if submit_context.set_strings else None,
        set_files=dict(submit_context.set_files) if submit_context.set_files else None,
        dataset_hints=list(submit_context.dataset_hints) if submit_context.dataset_hints else None,
        task_name_hints=list(submit_context.task_name_hints)
        if submit_context.task_name_hints
        else None,
    )
    return rewritten_command, rewritten_context, temp_output_path


def _create_prepared_workflow_temp_path(workflow_spec_path: Path) -> Path:
    suffix = workflow_spec_path.suffix or ".yaml"
    file_descriptor, temp_path = tempfile.mkstemp(
        prefix=f".{workflow_spec_path.stem}.roar-osmo-",
        suffix=suffix,
        dir=workflow_spec_path.parent,
        text=True,
    )
    os.close(file_descriptor)
    Path(temp_path).unlink(missing_ok=True)
    return Path(temp_path)


def _coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_osmo_input_artifacts(
    context: OsmoSubmitCommandContext,
    *,
    metadata_key: str,
) -> list[LocalRecordedArtifact]:
    artifact_specs: list[tuple[Path, dict[str, Any]]] = []

    if context.workflow_spec_path:
        workflow_path = Path(context.workflow_spec_path)
        if workflow_path.is_file():
            artifact_specs.append(
                (
                    workflow_path,
                    {
                        metadata_key: {
                            "role": "workflow_spec",
                            "argument": context.workflow_spec_argument,
                        }
                    },
                )
            )

    for key, raw_value in (context.set_files or {}).items():
        resolved = _resolve_local_path(raw_value, context.repo_root)
        if resolved is None:
            continue
        file_path = Path(resolved)
        if not file_path.is_file():
            continue
        artifact_specs.append(
            (
                file_path,
                {
                    metadata_key: {
                        "role": "set_file",
                        "key": key,
                        "argument": raw_value,
                    }
                },
            )
        )

    deduped_paths: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, metadata in artifact_specs:
        deduped_paths[str(path)] = (path, metadata)

    hashes = hash_files_blake3([path for path, _metadata in deduped_paths.values()])
    artifacts: list[LocalRecordedArtifact] = []
    for path_str, (path, metadata) in deduped_paths.items():
        digest = hashes.get(path_str)
        if not digest:
            continue
        artifacts.append(
            LocalRecordedArtifact(
                path=path_str,
                hashes={"blake3": digest},
                size=path.stat().st_size,
                metadata=json.dumps(metadata),
            )
        )
    return artifacts


def _build_submit_input_artifacts(
    context: OsmoSubmitCommandContext,
) -> list[LocalRecordedArtifact]:
    return _build_osmo_input_artifacts(context, metadata_key="osmo_submit_input")


def _resolve_local_path(value: str, base_dir: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    path = Path(text)
    if not path.is_absolute():
        if not base_dir:
            return None
        path = Path(base_dir) / path
    return str(path.resolve())


def _maybe_reconstitute_downloaded_lineage(
    *,
    config: dict[str, Any],
    download_result: OsmoOutputDownloadResult | None,
    repo_root: str,
    roar_dir: Path,
    job_uid: str,
    session_id: int | None,
    submit_step_number: int,
) -> OsmoLineageReconstitutionResult | None:
    if not bool(config.get("ingest_lineage_bundles", False)):
        return None
    if not bool(config.get("download_declared_outputs", False)):
        message = (
            "osmo.ingest_lineage_bundles requires osmo.download_declared_outputs = true "
            "so Roar can inspect downloaded output datasets for lineage bundles."
        )
        _emit_captured_output(f"[roar] {message}", sys.stderr)
        return OsmoLineageReconstitutionResult(error=message)
    if download_result is None or download_result.error:
        return OsmoLineageReconstitutionResult(
            error=download_result.error
            if download_result is not None
            else "no downloaded outputs available"
        )

    bundle_filename = str(config.get("lineage_bundle_filename", "roar-fragments.json"))
    try:
        bundles = discover_downloaded_lineage_bundles(
            download_result.datasets,
            bundle_filename=bundle_filename,
        )
    except ValueError as exc:
        message = str(exc)
        _emit_captured_output(f"[roar] {message}", sys.stderr)
        return OsmoLineageReconstitutionResult(error=message)

    if not bundles:
        return OsmoLineageReconstitutionResult()

    result = reconstitute_osmo_lineage_bundles(
        bundles=bundles,
        project_dir=repo_root,
        roar_db_path=roar_dir / "roar.db",
        driver_job_uid=job_uid,
        session_id=session_id,
        step_number=submit_step_number,
    )
    if result.error:
        _emit_captured_output(f"[roar] {result.error}", sys.stderr)
    elif result.fragments_processed:
        _emit_captured_output(
            "[roar] OSMO lineage reconstituted: "
            f"{result.jobs_merged} jobs, {result.artifacts_merged} artifacts",
            sys.stderr,
        )
    return result


def _download_declared_outputs(
    *,
    osmo_binary: str,
    repo_root: str,
    roar_dir: Path,
    submit_context: OsmoSubmitCommandContext,
    workflow_id: str | None,
    wait_result: OsmoWorkflowWaitResult | None,
    download_directory: str,
) -> OsmoOutputDownloadResult:
    if wait_result is None:
        message = (
            "osmo.download_declared_outputs requires a successful workflow query "
            "so Roar can resolve terminal workflow completion before downloading outputs."
        )
        _emit_captured_output(f"[roar] {message}", sys.stderr)
        return OsmoOutputDownloadResult(error=message)
    if wait_result is None or wait_result.status != "COMPLETED":
        return OsmoOutputDownloadResult(error="workflow did not complete successfully")

    declared_outputs = _resolve_declared_dataset_outputs(submit_context)
    if not declared_outputs:
        return OsmoOutputDownloadResult()

    base_dir = Path(download_directory)
    if not base_dir.is_absolute():
        base_dir = Path(repo_root) / base_dir
    workflow_dir = base_dir / (_sanitize_receipt_component(str(workflow_id or "")) or "submit")
    workflow_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[LocalRecordedArtifact] = []
    datasets: list[dict[str, Any]] = []
    for declared in declared_outputs:
        dataset_ref = f"{declared.dataset_name}:latest"
        dataset_dir = workflow_dir / (
            _sanitize_receipt_component(declared.dataset_name) or "dataset"
        )
        dataset_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                osmo_binary,
                "dataset",
                "download",
                dataset_ref,
                str(dataset_dir),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            details = _format_query_failure(result)
            message = f"failed to download declared dataset output {dataset_ref}: {details}"
            _emit_captured_output(f"[roar] {message}", sys.stderr)
            return OsmoOutputDownloadResult(artifacts=artifacts, datasets=datasets, error=message)

        dataset_files = [path for path in dataset_dir.rglob("*") if path.is_file()]
        hashes = hash_files_blake3(dataset_files)
        for file_path in dataset_files:
            digest = hashes.get(str(file_path))
            if not digest:
                continue
            artifacts.append(
                LocalRecordedArtifact(
                    path=str(file_path),
                    hashes={"blake3": digest},
                    size=file_path.stat().st_size,
                    source_type="osmo_dataset",
                    source_url=dataset_ref,
                    metadata=json.dumps(
                        {
                            "osmo_dataset_download": {
                                "dataset_name": declared.dataset_name,
                                "dataset_ref": dataset_ref,
                                "declared_path": declared.declared_path,
                                "task_name": declared.task_name,
                            }
                        }
                    ),
                )
            )

        datasets.append(
            {
                "dataset_name": declared.dataset_name,
                "dataset_ref": dataset_ref,
                "local_directory": str(dataset_dir),
                "file_count": len(dataset_files),
                "declared_path": declared.declared_path,
                "task_name": declared.task_name,
            }
        )

    return OsmoOutputDownloadResult(artifacts=artifacts, datasets=datasets)


def _capture_workflow_diagnostics(
    *,
    osmo_binary: str,
    repo_root: str,
    roar_dir: Path,
    submit_context: OsmoSubmitCommandContext,
    workflow_id: str | None,
    wait_result: OsmoWorkflowWaitResult | None,
) -> OsmoWorkflowDiagnosticsResult:
    if not workflow_id or wait_result is None or wait_result.payload is None:
        return OsmoWorkflowDiagnosticsResult()

    diagnostics_dir = (
        roar_dir / "osmo" / "diagnostics" / (_sanitize_receipt_component(workflow_id) or "submit")
    )
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[LocalRecordedArtifact] = []
    task_logs: list[dict[str, Any]] = []
    query_status = _sanitize_receipt_component(str(wait_result.status or "")) or "latest"
    query_path = diagnostics_dir / f"query-{query_status}.json"
    query_json = json.dumps(wait_result.payload, indent=2, sort_keys=True)
    query_path.write_text(f"{query_json}\n", encoding="utf-8")
    artifacts.append(
        _build_file_artifact(
            query_path,
            source_type="osmo_workflow_query",
            metadata={
                "osmo_workflow_query": {
                    "workflow_id": workflow_id,
                    "workflow_status": wait_result.status,
                }
            },
        )
    )

    error: str | None = None
    if (
        wait_result.status
        and wait_result.status != "COMPLETED"
        and _is_terminal_workflow_status(wait_result.status)
    ):
        for task_name in _resolve_workflow_task_names(submit_context):
            result = subprocess.run(
                [osmo_binary, "workflow", "logs", workflow_id, "--task", task_name],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            log_path = (
                diagnostics_dir
                / "tasks"
                / f"{_sanitize_receipt_component(task_name) or 'task'}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_text = result.stdout
            if result.stderr:
                if log_text and not log_text.endswith("\n"):
                    log_text += "\n"
                log_text += result.stderr
            log_path.write_text(log_text, encoding="utf-8")
            artifacts.append(
                _build_file_artifact(
                    log_path,
                    source_type="osmo_workflow_log",
                    metadata={
                        "osmo_workflow_log": {
                            "workflow_id": workflow_id,
                            "workflow_status": wait_result.status,
                            "task_name": task_name,
                            "return_code": result.returncode,
                        }
                    },
                )
            )
            task_logs.append(
                {
                    "task_name": task_name,
                    "path": str(log_path),
                    "return_code": result.returncode,
                }
            )
            if result.returncode != 0 and error is None:
                error = f"failed to capture logs for task {task_name}"

    return OsmoWorkflowDiagnosticsResult(
        artifacts=artifacts,
        query_artifact_path=str(query_path),
        task_logs=task_logs,
        error=error,
    )


def _resolve_declared_dataset_outputs(
    context: OsmoSubmitCommandContext,
) -> list[OsmoDeclaredDatasetOutput]:
    outputs: list[OsmoDeclaredDatasetOutput] = []
    loaded = _load_workflow_spec_data(context)
    if loaded is not None:
        parsed, replacements = loaded
        workflow = parsed.get("workflow", {})
        if isinstance(workflow, dict):
            tasks = workflow.get("tasks", [])
            if isinstance(tasks, list):
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    task_name = str(task.get("name") or "").strip() or None
                    task_outputs = task.get("outputs", [])
                    if not isinstance(task_outputs, list):
                        continue
                    for item in task_outputs:
                        if not isinstance(item, dict):
                            continue
                        dataset = item.get("dataset")
                        if not isinstance(dataset, dict):
                            continue
                        dataset_name = _render_submit_template(dataset.get("name"), replacements)
                        if not dataset_name:
                            continue
                        declared_path = _render_submit_template(dataset.get("path"), replacements)
                        outputs.append(
                            OsmoDeclaredDatasetOutput(
                                dataset_name=dataset_name,
                                declared_path=declared_path,
                                task_name=task_name,
                            )
                        )

    merged = _merge_declared_dataset_output_hints(outputs, context.dataset_hints)
    return merged


def _merge_declared_dataset_output_hints(
    outputs: list[OsmoDeclaredDatasetOutput],
    hints: list[str] | None,
) -> list[OsmoDeclaredDatasetOutput]:
    merged: list[OsmoDeclaredDatasetOutput] = list(outputs)
    seen_dataset_names = {
        str(item.dataset_name).strip() for item in outputs if str(item.dataset_name).strip()
    }
    for dataset_name in hints or []:
        normalized = str(dataset_name or "").strip()
        if not normalized or normalized in seen_dataset_names:
            continue
        merged.append(OsmoDeclaredDatasetOutput(dataset_name=normalized))
        seen_dataset_names.add(normalized)
    return merged


def _resolve_workflow_task_names(context: OsmoSubmitCommandContext) -> list[str]:
    task_names: list[str] = []
    loaded = _load_workflow_spec_data(context)
    if loaded is not None:
        parsed, replacements = loaded
        workflow = parsed.get("workflow", {})
        if isinstance(workflow, dict):
            tasks = workflow.get("tasks", [])
            if isinstance(tasks, list):
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    task_name = _render_submit_template(task.get("name"), replacements)
                    if task_name:
                        task_names.append(task_name)

    return _merge_workflow_task_name_hints(task_names, context.task_name_hints)


def _merge_workflow_task_name_hints(
    task_names: list[str],
    hints: list[str] | None,
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    for task_name in [*task_names, *(hints or [])]:
        normalized = str(task_name or "").strip()
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)

    return merged


def _load_workflow_spec_data(
    context: OsmoSubmitCommandContext,
) -> tuple[dict[str, Any], dict[str, str]] | None:
    workflow_spec_path = str(context.workflow_spec_path or "").strip()
    if not workflow_spec_path:
        return None

    raw_text = Path(workflow_spec_path).read_text(encoding="utf-8")
    normalized_text = re.sub(
        r":\s*{{\s*([A-Za-z0-9_.-]+)\s*}}(\s*(#.*)?)$",
        r': "__ROAR_OSMO_TEMPLATE_\1__"\2',
        raw_text,
        flags=re.MULTILINE,
    )
    parsed = yaml.safe_load(normalized_text)
    if not isinstance(parsed, dict):
        return None

    default_values = parsed.get("default-values", {})
    if not isinstance(default_values, dict):
        default_values = {}
    replacements: dict[str, str] = {
        str(key): str(value)
        for key, value in default_values.items()
        if value is not None and not isinstance(value, (dict, list))
    }
    for key, value in (context.set_strings or {}).items():
        replacements[str(key)] = str(value)
    return parsed, replacements


def _render_submit_template(value: Any, replacements: dict[str, str]) -> str | None:
    if value is None:
        return None
    text = str(value)
    rendered = _OSMO_TEMPLATE_SENTINEL_RE.sub(
        lambda match: replacements.get(match.group(1), match.group(0)),
        text,
    )
    rendered = re.sub(
        r"{{\s*([A-Za-z0-9_.-]+)\s*}}",
        lambda match: replacements.get(match.group(1), match.group(0)),
        rendered,
    ).strip()
    if "{{" in rendered or "}}" in rendered:
        return None
    if _OSMO_TEMPLATE_SENTINEL_RE.search(rendered):
        return None
    return rendered or None


def _build_file_artifact(
    path: Path,
    *,
    source_type: str | None = None,
    source_url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> LocalRecordedArtifact:
    hashes = hash_files_blake3([path])
    return LocalRecordedArtifact(
        path=str(path),
        hashes={"blake3": hashes[str(path)]},
        size=path.stat().st_size,
        source_type=source_type,
        source_url=source_url,
        metadata=json.dumps(metadata) if metadata is not None else None,
    )


def _update_recorded_osmo_submit(
    *,
    db_ctx: Any,
    job_id: int,
    metadata: str | None,
    receipt_artifact: LocalRecordedArtifact,
) -> None:
    if metadata is not None:
        db_ctx.jobs.update_metadata(job_id, metadata)
        refresh_job_system_labels(
            db_ctx,
            job_id=job_id,
            job=db_ctx.jobs.get(job_id),
        )

    artifact_id, _created = db_ctx.artifacts.register(
        hashes=receipt_artifact.hashes,
        size=receipt_artifact.size,
        path=receipt_artifact.path,
        source_type=receipt_artifact.source_type,
        source_url=receipt_artifact.source_url,
        metadata=receipt_artifact.metadata,
    )
    db_ctx.jobs.add_output(job_id, artifact_id, receipt_artifact.path)


def _extract_workflow_id(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    for key in ("name", "workflow_id", "id", "workflowUuid", "workflow_uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_git_commit(repo_root: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _query_workflow_state(
    *,
    osmo_binary: str,
    repo_root: str,
    workflow_id: str | None,
) -> OsmoWorkflowWaitResult:
    if not workflow_id:
        return OsmoWorkflowWaitResult(error="missing workflow id")

    result = subprocess.run(
        [osmo_binary, "workflow", "query", workflow_id, "--format-type", "json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return OsmoWorkflowWaitResult(error=_format_query_failure(result))

    payload = _parse_json_response(result.stdout)
    if payload is None:
        return OsmoWorkflowWaitResult(error="OSMO workflow query did not return JSON output")

    status = str(payload.get("status") or "").strip().upper() or None
    return OsmoWorkflowWaitResult(status=status, payload=payload)


def _wait_for_workflow_completion(
    *,
    command: list[str],
    repo_root: str,
    workflow_id: str | None,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> OsmoWorkflowWaitResult:
    if not workflow_id:
        _emit_captured_output(
            "[roar] OSMO submit did not return a workflow id; cannot wait for completion.",
            sys.stderr,
        )
        return OsmoWorkflowWaitResult(error="missing workflow id")

    _emit_captured_output(
        f"[roar] waiting for OSMO workflow {workflow_id} to reach a terminal state...",
        sys.stderr,
    )
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None

    while time.monotonic() < deadline:
        query_result = _query_workflow_state(
            osmo_binary=command[0],
            repo_root=repo_root,
            workflow_id=workflow_id,
        )
        if query_result.error:
            last_error = query_result.error
        elif _is_terminal_workflow_status(str(query_result.status or "")):
            _emit_captured_output(
                f"[roar] OSMO workflow {workflow_id} finished with status {query_result.status}.",
                sys.stderr,
            )
            return query_result
        time.sleep(poll_interval_seconds)

    message = f"[roar] timed out waiting for OSMO workflow {workflow_id} after {timeout_seconds}s."
    if last_error:
        message = f"{message} last_error={last_error}"
    _emit_captured_output(message, sys.stderr)
    return OsmoWorkflowWaitResult(timed_out=True, error=last_error)


def _resolve_final_exit_code(submit_return_code: int, wait_result: OsmoWorkflowWaitResult) -> int:
    if submit_return_code != 0:
        return submit_return_code
    if wait_result.timed_out or wait_result.error:
        return 1
    if wait_result.status and wait_result.status != "COMPLETED":
        return 1
    return 0


def _resolve_attach_exit_code(wait_result: OsmoWorkflowWaitResult) -> int:
    if wait_result.timed_out or wait_result.error:
        return 1
    return 0


def _format_query_failure(result: subprocess.CompletedProcess[str]) -> str:
    stderr = _truncate_output(result.stderr)
    stdout = _truncate_output(result.stdout)
    details = stderr or stdout or "unknown error"
    return f"query rc={result.returncode}: {details}"


def _is_terminal_workflow_status(status: str) -> bool:
    normalized = str(status or "").strip().upper()
    return normalized in _TERMINAL_WORKFLOW_STATUSES or normalized.startswith("FAILED")


def _truncate_output(text: str, limit: int = 2000) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit]}..."


def _write_osmo_submit_receipt(
    *,
    roar_dir: Path,
    payload: dict[str, Any],
) -> LocalRecordedArtifact:
    return _write_osmo_workflow_receipt(
        roar_dir=roar_dir,
        payload=payload,
        receipt_dir_name="submissions",
        payload_key="osmo_submit",
    )


def _write_osmo_attach_receipt(
    *,
    roar_dir: Path,
    payload: dict[str, Any],
) -> LocalRecordedArtifact:
    return _write_osmo_workflow_receipt(
        roar_dir=roar_dir,
        payload=payload,
        receipt_dir_name="attachments",
        payload_key="osmo_attach",
    )


def _write_osmo_workflow_receipt(
    *,
    roar_dir: Path,
    payload: dict[str, Any],
    receipt_dir_name: str,
    payload_key: str,
) -> LocalRecordedArtifact:
    receipt_dir = roar_dir / "osmo" / receipt_dir_name
    receipt_dir.mkdir(parents=True, exist_ok=True)
    workflow_id = str(payload.get("workflow_id") or "").strip()
    status = str(payload.get("workflow_status") or "").strip()
    filename_parts = [
        _sanitize_receipt_component(workflow_id) or "submit",
    ]
    if status:
        filename_parts.append(_sanitize_receipt_component(status))
    receipt_path = receipt_dir / f"{'-'.join(filename_parts)}.json"
    receipt_json = json.dumps(
        {payload_key: payload},
        indent=2,
        sort_keys=True,
    )
    receipt_path.write_text(f"{receipt_json}\n", encoding="utf-8")
    hashes = hash_files_blake3([receipt_path])
    return LocalRecordedArtifact(
        path=str(receipt_path),
        hashes={"blake3": hashes[str(receipt_path)]},
        size=receipt_path.stat().st_size,
        source_type="osmo_workflow_receipt",
        metadata=json.dumps(
            {
                "osmo_workflow_receipt": {
                    "workflow_id": workflow_id or None,
                    "workflow_status": status or None,
                    "receipt_dir": receipt_dir_name,
                }
            }
        ),
    )


def _sanitize_receipt_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    normalized = normalized.strip("-._")
    return normalized[:120]


__all__ = [
    "OsmoAttachOptions",
    "attach_osmo_workflow",
    "execute_osmo_workflow_submit",
]
