"""Application orchestration for the local show query."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from ...core.bootstrap import bootstrap
from ...core.logging import get_logger
from ...db.context import create_database_context, optional_repo
from ...presenters.show_renderer import ShowRenderer
from .requests import ShowQueryRequest
from .results import (
    ShowArtifactComponentSummary,
    ShowArtifactJobSummary,
    ShowArtifactLocationSummary,
    ShowArtifactSummary,
    ShowHashSummary,
    ShowJobArtifactSummary,
    ShowJobSummary,
    ShowSessionJobSummary,
    ShowSessionSummary,
    ShowSummary,
)


class ShowQueryError(RuntimeError):
    """Raised when a show query cannot build a summary."""


def render_show(request: ShowQueryRequest) -> str:
    """Render session, job, or artifact details."""
    try:
        summary = build_show_summary(request)
    except ShowQueryError as exc:
        return str(exc)

    renderer = ShowRenderer()
    if isinstance(summary, ShowSessionSummary):
        session, jobs, labels = summary.to_renderer_args()
        return renderer.render_session(session, jobs, labels=labels)
    if isinstance(summary, ShowJobSummary):
        job, inputs, outputs, labels = summary.to_renderer_args()
        return renderer.render_job(job, inputs, outputs, labels=labels)

    artifact, locations, related_jobs, labels, composite_summary, components = (
        summary.to_renderer_args()
    )
    return renderer.render_artifact(
        artifact,
        locations,
        related_jobs,
        labels=labels,
        composite_summary=composite_summary,
        components=components,
    )


def build_show_summary(request: ShowQueryRequest) -> ShowSummary:
    """Build a typed show summary for session, job, or artifact details."""
    bootstrap(request.roar_dir)
    logger = _logger()
    if logger:
        logger.debug("show: entry with ref=%r", request.ref)

    with create_database_context(request.roar_dir) as db_ctx:
        if request.ref is None:
            session = db_ctx.sessions.get_active()
            if not session:
                raise ShowQueryError("No active session.")
            return _build_session_summary(db_ctx, session)

        ref_type = _classify_ref(request.ref, request.cwd)
        if logger:
            logger.debug("show: ref_type=%r for ref=%r", ref_type, request.ref)

        if ref_type == "job_step":
            session = db_ctx.sessions.get_active()
            if not session:
                raise ShowQueryError("No active session.")
            job = _resolve_job_ref(db_ctx, int(session["id"]), request.ref)
            if not job:
                raise ShowQueryError(f"Job not found: {request.ref}")
            return _build_job_summary(db_ctx, job)

        if ref_type == "file_path":
            path_obj = Path(os.path.expanduser(request.ref))
            if not path_obj.is_absolute():
                path_obj = request.cwd / path_obj
            resolved_path = os.path.normpath(str(path_obj.absolute()))
            artifact = db_ctx.artifacts.get_by_path(resolved_path)
            if not artifact:
                raise ShowQueryError(f"No artifact found for path: {request.ref}")
            return _build_artifact_summary(db_ctx, artifact)

        if ref_type == "job_uid":
            job = db_ctx.jobs.get_by_uid(request.ref)
            if not job:
                raise ShowQueryError(f"Job not found: {request.ref}")
            return _build_job_summary(db_ctx, job)

        if ref_type == "artifact_hash":
            job = db_ctx.jobs.get_by_uid(request.ref)
            if job:
                return _build_job_summary(db_ctx, job)
            artifact = db_ctx.artifacts.get_by_hash(request.ref)
            if artifact:
                return _build_artifact_summary(db_ctx, artifact)
            raise ShowQueryError(f"Not found: {request.ref}")

        raise ShowQueryError(f"Unknown reference format: {request.ref}")


def _logger():
    return get_logger()


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


def _build_session_summary(db_ctx, session: dict[str, Any]) -> ShowSessionSummary:
    jobs = db_ctx.jobs.get_by_session(session["id"], limit=100)
    labels = _current_label_metadata(db_ctx, "dag", session_id=int(session["id"]))
    return ShowSessionSummary(
        hash=str(session["hash"]),
        created_at=session["created_at"],
        git_repo=session.get("git_repo"),
        git_commit_start=session.get("git_commit_start"),
        labels=labels,
        jobs=[
            ShowSessionJobSummary(
                step_number=job.get("step_number"),
                job_uid=job.get("job_uid"),
                exit_code=job.get("exit_code"),
                command=job.get("command"),
                job_type=job.get("job_type"),
            )
            for job in jobs
        ],
    )


def _build_job_summary(db_ctx, job: dict[str, Any]) -> ShowJobSummary:
    inputs = db_ctx.jobs.get_inputs(job["id"])
    outputs = db_ctx.jobs.get_outputs(job["id"])
    prepared = _prepare_job_for_render(job)
    labels = _current_label_metadata(db_ctx, "job", job_id=int(job["id"]))
    return ShowJobSummary(
        job_uid=str(prepared["job_uid"]),
        step_number=prepared.get("step_number"),
        job_type=prepared.get("job_type"),
        step_name=prepared.get("step_name"),
        step_identity=prepared.get("step_identity"),
        timestamp=prepared["timestamp"],
        duration_seconds=prepared.get("duration_seconds"),
        exit_code=prepared.get("exit_code"),
        command=prepared.get("command"),
        git_commit=prepared.get("git_commit"),
        git_branch=prepared.get("git_branch"),
        metadata=cast(dict[str, Any] | None, prepared.get("metadata")),
        telemetry=cast(dict[str, Any] | None, prepared.get("telemetry")),
        labels=labels,
        inputs=[_build_job_artifact_summary(artifact) for artifact in inputs],
        outputs=[_build_job_artifact_summary(artifact) for artifact in outputs],
    )


def _build_job_artifact_summary(artifact: dict[str, Any]) -> ShowJobArtifactSummary:
    return ShowJobArtifactSummary(
        path=str(artifact["path"]),
        artifact_id=str(artifact["artifact_id"]),
        kind=cast(str | None, artifact.get("kind")),
        component_count=cast(int | None, artifact.get("component_count")),
        size=int(artifact["size"]),
        hashes=[
            ShowHashSummary(
                algorithm=str(hash_entry["algorithm"]),
                digest=str(hash_entry["digest"]),
            )
            for hash_entry in cast(list[dict[str, Any]], artifact.get("hashes", []))
        ],
    )


def _build_artifact_summary(db_ctx, artifact: dict[str, Any]) -> ShowArtifactSummary:
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

    metadata = cast(dict[str, Any] | None, artifact.get("metadata"))
    if isinstance(metadata, str):
        metadata = _safe_json_loads(metadata, "artifact metadata")

    return ShowArtifactSummary(
        id=str(artifact["id"]),
        kind=cast(str | None, artifact.get("kind")),
        component_count=cast(int | None, artifact.get("component_count")),
        size=int(artifact["size"]),
        first_seen_at=artifact["first_seen_at"],
        first_seen_path=cast(str | None, artifact.get("first_seen_path")),
        labels=labels,
        composite_summary=cast(dict[str, Any] | None, composite_summary),
        metadata=metadata,
        hashes=[
            ShowHashSummary(
                algorithm=str(hash_entry["algorithm"]),
                digest=str(hash_entry["digest"]),
            )
            for hash_entry in cast(list[dict[str, Any]], artifact.get("hashes", []))
        ],
        locations=[
            ShowArtifactLocationSummary(path=str(location["path"])) for location in locations
        ],
        produced_by=[
            ShowArtifactJobSummary(
                job_uid=cast(str | None, job_summary.get("job_uid")),
                command=cast(str | None, job_summary.get("command")),
            )
            for job_summary in cast(list[dict[str, Any]], jobs.get("produced_by", []))
        ],
        consumed_by=[
            ShowArtifactJobSummary(
                job_uid=cast(str | None, job_summary.get("job_uid")),
                command=cast(str | None, job_summary.get("command")),
            )
            for job_summary in cast(list[dict[str, Any]], jobs.get("consumed_by", []))
        ],
        components=[
            ShowArtifactComponentSummary(
                relative_path=cast(str | None, component.get("relative_path")),
                component_digest=cast(str | None, component.get("component_digest")),
                leaf_kind=cast(str | None, component.get("leaf_kind")),
            )
            for component in cast(list[dict[str, Any]], components or [])
        ],
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
