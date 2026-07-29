"""Host-side execution for kubectl workload submits."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from roar.backends.k8s.config import load_k8s_backend_config
from roar.backends.k8s.submit import (
    K8sSubmitContext,
    _find_filename_argument,
    discard_submit_context,
    load_submit_context,
)
from roar.backends.k8s.workload_wait import (
    extract_kubectl_global_flags,
    wait_for_workload_completion,
)
from roar.core.bootstrap import bootstrap
from roar.core.models.run import RunContext, RunResult, resolve_run_config_start_dir
from roar.core.operation_metadata import build_operation_metadata_json
from roar.db.context import create_database_context
from roar.db.hashing import hash_files_blake3
from roar.execution.recording import LocalJobRecorder, LocalRecordedArtifact
from roar.execution.runtime.errors import ExecutionSetupError


def execute_k8s_job_submit(ctx: RunContext) -> RunResult:
    """Submit a (possibly instrumented) Job manifest and record it locally."""
    bootstrap(ctx.roar_dir)
    started_at = time.time()
    config = load_k8s_backend_config(start_dir=str(resolve_run_config_start_dir(ctx)))

    filename = _find_filename_argument(ctx.command)
    prepared_path = Path(filename[1]).resolve() if filename else None
    submit_context = load_submit_context(prepared_path) if prepared_path else None

    try:
        completed = subprocess.run(
            ctx.command,
            cwd=ctx.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ExecutionSetupError(
            "Error: kubectl not found. Install kubectl or adjust PATH."
        ) from exc
    finally:
        if submit_context is not None and prepared_path is not None:
            # The prepared manifest embeds the fragment token Secret.
            prepared_path.unlink(missing_ok=True)
            discard_submit_context(prepared_path)

    _emit_captured_output(completed.stdout, sys.stdout)
    _emit_captured_output(completed.stderr, sys.stderr)

    final_exit_code = completed.returncode
    wait_payload: dict[str, Any] | None = None
    if (
        completed.returncode == 0
        and submit_context is not None
        and bool(config.get("wait_for_completion", True))
    ):
        succeeded, wait_payload = wait_for_workload_completion(
            kubectl_binary=ctx.command[0],
            global_flags=extract_kubectl_global_flags(ctx.command),
            kubectl_resource=submit_context.kubectl_resource,
            name=submit_context.job_name,
            namespace=submit_context.namespace,
            timeout_seconds=int(config.get("wait_timeout_seconds", 30 * 60)),
            poll_interval_seconds=float(config.get("poll_interval_seconds", 5.0)),
        )
        if not succeeded:
            final_exit_code = 1

    recorded_command = (
        shlex.join(submit_context.original_command)
        if submit_context is not None
        else shlex.join(ctx.command)
    )
    payload = _build_submit_payload(
        ctx=ctx,
        submit_context=submit_context,
        exit_code=final_exit_code,
        submit_stdout=completed.stdout,
        wait_payload=wait_payload,
    )
    duration = max(0.0, time.time() - started_at)

    with create_database_context(ctx.roar_dir) as db_ctx:
        session_id = db_ctx.sessions.get_or_create_active()
        recorder = LocalJobRecorder()
        job_id, job_uid = recorder.record(
            db_ctx,
            command=recorded_command,
            timestamp=started_at,
            metadata=build_operation_metadata_json("k8s_submit", payload),
            execution_backend=ctx.execution_backend,
            execution_role=ctx.execution_role,
            job_type=ctx.job_type or "run",
            input_artifacts=_build_submit_input_artifacts(submit_context),
            output_artifacts=[],
            duration_seconds=duration,
            exit_code=final_exit_code,
            session_id=session_id,
            job_uid=submit_context.parent_job_uid if submit_context else None,
        )
        inputs = db_ctx.jobs.get_inputs(job_id)
        outputs = db_ctx.jobs.get_outputs(job_id)
        db_ctx.commit()

    return RunResult(
        exit_code=final_exit_code,
        job_id=job_id,
        job_uid=job_uid,
        duration=duration,
        inputs=inputs,
        outputs=outputs,
        interrupted=False,
        is_build=ctx.job_type == "build",
        backend="k8s",
    )


def _build_submit_input_artifacts(
    submit_context: K8sSubmitContext | None,
) -> list[LocalRecordedArtifact]:
    if submit_context is None:
        return []
    manifest_path = Path(submit_context.manifest_path)
    if not manifest_path.is_file():
        return []

    hashes = hash_files_blake3([manifest_path])
    digest = hashes.get(str(manifest_path))
    if not digest:
        return []
    return [
        LocalRecordedArtifact(
            path=str(manifest_path),
            hashes={"blake3": digest},
            size=manifest_path.stat().st_size,
            metadata=json.dumps({"k8s_submit_input": {"role": "job_manifest"}}),
        )
    ]


def _build_submit_payload(
    *,
    ctx: RunContext,
    submit_context: K8sSubmitContext | None,
    exit_code: int,
    submit_stdout: str,
    wait_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "executed_command": shlex.join(ctx.command),
        "exit_code": exit_code,
        "instrumented": submit_context is not None,
        "submit_stdout_tail": submit_stdout.strip().splitlines()[-5:],
    }
    if submit_context is not None:
        payload.update(
            {
                "workload_kind": submit_context.workload_kind,
                "job_name": submit_context.job_name,
                "namespace": submit_context.namespace,
                "manifest_path": submit_context.manifest_path,
                "secret_name": submit_context.secret_name,
                "session_id": submit_context.session_id,
                "parent_job_uid": submit_context.parent_job_uid,
                "wrapped_containers": submit_context.wrapped_containers,
                "skipped_containers": submit_context.skipped_containers,
            }
        )
    if wait_payload is not None:
        payload["wait"] = wait_payload
    return payload


def _emit_captured_output(text: str, stream: Any) -> None:
    if text:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")
        stream.flush()


__all__ = ["execute_k8s_job_submit"]
