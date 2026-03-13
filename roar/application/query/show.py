"""Application orchestration for the local show query."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from ...core.bootstrap import bootstrap
from ...core.di import try_resolve
from ...core.interfaces.logger import ILogger
from ...db.context import create_database_context, optional_repo
from ...presenters.show_renderer import ShowRenderer
from .requests import ShowQueryRequest


def render_show(request: ShowQueryRequest) -> str:
    """Render session, job, or artifact details."""
    bootstrap(request.roar_dir)
    logger = _logger()
    if logger:
        logger.debug("show: entry with ref=%r", request.ref)

    with create_database_context(request.roar_dir) as db_ctx:
        if request.ref is None:
            session = db_ctx.sessions.get_active()
            if not session:
                return "No active session."
            return _render_session(db_ctx, session)

        ref_type = _classify_ref(request.ref, request.cwd)
        if logger:
            logger.debug("show: ref_type=%r for ref=%r", ref_type, request.ref)

        if ref_type == "job_step":
            session = db_ctx.sessions.get_active()
            if not session:
                return "No active session."
            job = _resolve_job_ref(db_ctx, int(session["id"]), request.ref)
            if not job:
                return f"Job not found: {request.ref}"
            return _render_job(db_ctx, job)

        if ref_type == "file_path":
            path_obj = Path(os.path.expanduser(request.ref))
            if not path_obj.is_absolute():
                path_obj = request.cwd / path_obj
            resolved_path = os.path.normpath(str(path_obj.absolute()))
            artifact = db_ctx.artifacts.get_by_path(resolved_path)
            if not artifact:
                return f"No artifact found for path: {request.ref}"
            return _render_artifact(db_ctx, artifact)

        if ref_type == "job_uid":
            job = db_ctx.jobs.get_by_uid(request.ref)
            if not job:
                return f"Job not found: {request.ref}"
            return _render_job(db_ctx, job)

        if ref_type == "artifact_hash":
            job = db_ctx.jobs.get_by_uid(request.ref)
            if job:
                return _render_job(db_ctx, job)
            artifact = db_ctx.artifacts.get_by_hash(request.ref)
            if artifact:
                return _render_artifact(db_ctx, artifact)
            return f"Not found: {request.ref}"

        return f"Unknown reference format: {request.ref}"


def _logger() -> ILogger | None:
    return try_resolve(ILogger)  # type: ignore[type-abstract]


def _safe_json_loads(raw: str, context: str) -> dict | None:
    logger = _logger()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        if logger:
            logger.debug("show: failed to decode %s JSON: %s", context, exc)
        return None
    if not isinstance(parsed, dict):
        if logger:
            logger.debug(
                "show: expected object for %s JSON, got %s", context, type(parsed).__name__
            )
        return None
    return parsed


def _classify_ref(ref: str, cwd: Path) -> str:
    if ref.startswith("@"):
        return "job_step"
    if "/" in ref or ref.startswith(("./", "../", "~")):
        return "file_path"
    if (cwd / ref).exists():
        return "file_path"
    is_hex = all(char in "0123456789abcdefABCDEF" for char in ref)
    if is_hex and len(ref) <= 8:
        return "job_uid"
    if is_hex and len(ref) > 8:
        return "artifact_hash"
    return "unknown"


def _resolve_job_ref(db_ctx, session_id: int, job_ref: str) -> dict | None:
    if job_ref.startswith("@"):
        ref = job_ref[1:]
        job_type = None
        if ref.startswith("B"):
            job_type = "build"
            ref = ref[1:]
        try:
            step_number = int(ref)
        except ValueError:
            return None
        return db_ctx.sessions.get_step_by_number(session_id, step_number, job_type)
    return db_ctx.jobs.get_by_uid(job_ref)


def _prepare_job_for_render(job: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(job)
    if prepared.get("metadata") and isinstance(prepared["metadata"], str):
        prepared["metadata"] = _safe_json_loads(prepared["metadata"], "metadata")
    if prepared.get("telemetry") and isinstance(prepared["telemetry"], str):
        prepared["telemetry"] = _safe_json_loads(prepared["telemetry"], "telemetry")
    return prepared


def _render_session(db_ctx, session: dict[str, Any]) -> str:
    jobs = db_ctx.jobs.get_by_session(session["id"], limit=100)
    labels = _current_label_metadata(db_ctx, "dag", session_id=int(session["id"]))
    return ShowRenderer().render_session(session, jobs, labels=labels)


def _render_job(db_ctx, job: dict[str, Any]) -> str:
    inputs = db_ctx.jobs.get_inputs(job["id"])
    outputs = db_ctx.jobs.get_outputs(job["id"])
    prepared = _prepare_job_for_render(job)
    labels = _current_label_metadata(db_ctx, "job", job_id=int(job["id"]))
    return ShowRenderer().render_job(prepared, inputs, outputs, labels=labels)


def _render_artifact(db_ctx, artifact: dict[str, Any]) -> str:
    locations = db_ctx.artifacts.get_locations(artifact["id"])
    jobs = db_ctx.artifacts.get_jobs(artifact["id"])
    labels = _current_label_metadata(db_ctx, "artifact", artifact_id=str(artifact["id"]))

    composite_summary = None
    components = None
    composite_repo = cast(Any, optional_repo(db_ctx, "composites"))
    if composite_repo is not None:
        summary = composite_repo.get(artifact["id"])
        if isinstance(summary, dict):
            composite_summary = summary
            components = composite_repo.get_components(artifact["id"], limit=10) or None

    return ShowRenderer().render_artifact(
        artifact,
        locations,
        jobs,
        labels=labels,
        composite_summary=composite_summary,
        components=components,
    )


def _current_label_metadata(
    db_ctx,
    entity_type: str,
    *,
    session_id: int | None = None,
    job_id: int | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any] | None:
    labels_repo = cast(Any, optional_repo(db_ctx, "labels"))
    if labels_repo is None:
        return None

    current = labels_repo.get_current(
        entity_type,
        session_id=session_id,
        job_id=job_id,
        artifact_id=artifact_id,
    )
    if not isinstance(current, dict):
        return None

    metadata = current.get("metadata")
    return metadata if isinstance(metadata, dict) else None
