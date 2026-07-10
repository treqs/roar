"""Shared export of a locally recorded job as an execution-fragment bundle.

Backend-neutral: OSMO and Kubernetes runtime wrappers both export the
job that `roar run` recorded inside a remote task/pod as a single
``ExecutionFragment`` bundle, either returned as a file (OSMO datasets)
or streamed to GLaaS (k8s fragment sessions).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roar.db.context import create_database_context
from roar.execution.fragments.models import ArtifactRef, ExecutionFragment


@dataclass(frozen=True)
class LocalJobFragmentExport:
    output_path: str
    exported_job_uid: str
    fragment_count: int
    task_id: str
    task_name: str


def export_local_job_fragment_bundle(
    *,
    roar_dir: Path,
    output_path: Path,
    backend_name: str,
    job_uid: str | None = None,
    task_id: str | None = None,
    task_name: str | None = None,
    parent_job_uid: str = "",
    default_task_name: str = "task",
) -> LocalJobFragmentExport:
    project_dir = str(roar_dir.parent.resolve())

    with create_database_context(roar_dir) as db_ctx:
        selected_job = _select_export_job(db_ctx, job_uid=job_uid)
        if selected_job is None:
            raise ValueError("no local Roar jobs are available to export")

        selected_job_id = int(selected_job["id"])
        selected_job_uid = str(selected_job.get("job_uid") or "").strip()
        resolved_task_id = str(task_id or selected_job_uid).strip()
        if not resolved_task_id:
            raise ValueError("task_id is required when exporting a job without a persisted job_uid")

        resolved_task_name = _resolve_task_name(
            selected_job,
            task_name,
            default_task_name=default_task_name,
        )
        reads = [
            _build_artifact_ref(item, project_dir=project_dir)
            for item in db_ctx.jobs.get_inputs(selected_job_id)
        ]
        writes = [
            _build_artifact_ref(item, project_dir=project_dir)
            for item in db_ctx.jobs.get_outputs(selected_job_id)
        ]

    started_at = float(selected_job.get("timestamp") or 0.0)
    duration = max(0.0, float(selected_job.get("duration_seconds") or 0.0))
    fragment = ExecutionFragment(
        job_uid=selected_job_uid or resolved_task_id,
        parent_job_uid=parent_job_uid,
        task_id=resolved_task_id,
        worker_id="",
        node_id="",
        actor_id=None,
        task_name=resolved_task_name,
        started_at=started_at,
        ended_at=started_at + duration,
        exit_code=int(selected_job.get("exit_code") or 0),
        backend=str(backend_name or "").strip() or "local",
        reads=reads,
        writes=writes,
        backend_metadata={
            "execution_role": "task",
            "source_job_uid": selected_job_uid or None,
            "source_execution_backend": str(selected_job.get("execution_backend") or "").strip()
            or None,
            "source_execution_role": str(selected_job.get("execution_role") or "").strip() or None,
            "source_job_type": str(selected_job.get("job_type") or "").strip() or None,
        },
    )

    payload = {
        "fragments": [fragment.to_dict()],
        "metadata": {
            "exported_job_uid": selected_job_uid or None,
            "task_id": resolved_task_id,
            "task_name": resolved_task_name,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return LocalJobFragmentExport(
        output_path=str(output_path),
        exported_job_uid=selected_job_uid,
        fragment_count=1,
        task_id=resolved_task_id,
        task_name=resolved_task_name,
    )


def _select_export_job(db_ctx, *, job_uid: str | None) -> dict[str, Any] | None:
    if job_uid:
        selected = db_ctx.jobs.get_by_uid(job_uid)
        if selected is None:
            raise ValueError(f"local Roar job not found for job UID {job_uid!r}")
        return selected

    jobs = db_ctx.jobs.get_recent(limit=1)
    if not jobs:
        return None
    return jobs[0]


def _resolve_task_name(
    job: dict[str, Any],
    requested_task_name: str | None,
    *,
    default_task_name: str,
) -> str:
    explicit = str(requested_task_name or "").strip()
    if explicit:
        return explicit

    script = str(job.get("script") or "").strip()
    if script:
        return script

    command = str(job.get("command") or "").strip()
    if not command:
        return default_task_name

    first_token = command.split(" ", 1)[0].strip()
    return first_token or default_task_name


def _build_artifact_ref(payload: dict[str, Any], *, project_dir: str) -> ArtifactRef:
    hash_value, hash_algorithm = _select_primary_hash(payload.get("hashes"))
    path = _normalize_bundle_path(str(payload.get("path") or ""), project_dir=project_dir)
    return ArtifactRef(
        path=path,
        hash=hash_value,
        hash_algorithm=hash_algorithm,
        size=int(payload.get("size") or 0),
        capture_method="python",
    )


def _select_primary_hash(hashes: Any) -> tuple[str | None, str]:
    if isinstance(hashes, list):
        blake3_row = next(
            (
                item
                for item in hashes
                if isinstance(item, dict) and str(item.get("algorithm") or "").strip() == "blake3"
            ),
            None,
        )
        if isinstance(blake3_row, dict):
            digest = str(blake3_row.get("digest") or "").strip()
            if digest:
                return digest, "blake3"

        for item in hashes:
            if not isinstance(item, dict):
                continue
            algorithm = str(item.get("algorithm") or "").strip()
            digest = str(item.get("digest") or "").strip()
            if algorithm and digest:
                return digest, algorithm

    return None, "blake3"


def _normalize_bundle_path(path: str, *, project_dir: str) -> str:
    normalized = str(path or "").strip()
    if not normalized or "://" in normalized:
        return normalized

    candidate = Path(normalized)
    project_root = Path(project_dir)
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve(strict=False)

    try:
        relative = candidate.resolve(strict=False).relative_to(project_root)
    except ValueError:
        return str(candidate)

    return "${ROAR_PROJECT_DIR}/" + relative.as_posix()


__all__ = [
    "LocalJobFragmentExport",
    "export_local_job_fragment_bundle",
]
