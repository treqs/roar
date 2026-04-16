"""Reference parsing and local target resolution for `roar diff`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from ...db.query_context import QueryDatabaseContext
from ..lookup import RefKind
from ..lookup import classify_ref as classify_lookup_ref

DiffRefType = Literal[
    "session",
    "job_step",
    "file_path",
    "job_uid",
    "artifact_hash",
    "path_candidate",
]

_SESSION_PREFIX = "session:"


class DiffError(RuntimeError):
    """Raised when a diff query cannot be completed."""


def classify_diff_ref(ref: str) -> DiffRefType:
    """Classify a diff reference string by the resolver path it needs."""
    if ref.startswith(_SESSION_PREFIX):
        return "session"

    ref_kind = classify_lookup_ref(ref)
    if ref_kind == RefKind.JOB_STEP:
        return "job_step"
    if ref_kind == RefKind.FILE_PATH:
        return "file_path"
    if ref_kind == RefKind.JOB_UID:
        return "job_uid"
    if ref_kind == RefKind.ARTIFACT_HASH:
        return "artifact_hash"
    return "path_candidate"


def resolve_ref_to_artifact_id(
    db_ctx: QueryDatabaseContext,
    ref: str,
    cwd: Path,
) -> tuple[str, str | None]:
    """Resolve a local diff reference to ``(artifact_id, display_path)``.

    For job refs (@N, uid), returns the first output artifact.
    For artifact refs (hash, path), returns the artifact directly.
    """
    ref_type = classify_diff_ref(ref)

    if ref_type == "job_step":
        return resolve_job_ref_to_artifact(db_ctx, ref)

    if ref_type == "job_uid":
        job = db_ctx.jobs.get_by_uid(ref)
        if job:
            outputs = db_ctx.jobs.get_outputs(job["id"])
            if outputs:
                return str(outputs[0]["artifact_id"]), outputs[0].get("path")
        artifact = db_ctx.artifacts.get_by_hash(ref)
        if artifact:
            return str(artifact["id"]), artifact.get("first_seen_path")
        raise DiffError(f"Not found: {ref}")

    if ref_type == "file_path":
        return resolve_path_to_artifact(db_ctx, ref, cwd)

    if ref_type == "artifact_hash":
        job = db_ctx.jobs.get_by_uid(ref)
        if job:
            outputs = db_ctx.jobs.get_outputs(job["id"])
            if outputs:
                return str(outputs[0]["artifact_id"]), outputs[0].get("path")
        artifact = db_ctx.artifacts.get_by_hash(ref)
        if artifact:
            return str(artifact["id"]), artifact.get("first_seen_path")
        raise DiffError(f"Artifact not found: {ref}")

    return resolve_path_to_artifact(db_ctx, ref, cwd)


def resolve_job_ref_to_artifact(
    db_ctx: QueryDatabaseContext,
    ref: str,
) -> tuple[str, str | None]:
    """Resolve ``@N`` / ``@BN`` references to the first output artifact."""
    session = db_ctx.sessions.get_active()
    if not session:
        raise DiffError("No active session.")

    step_ref = ref[1:]
    job_type = None
    if step_ref.startswith("B"):
        job_type = "build"
        step_ref = step_ref[1:]

    try:
        step_number = int(step_ref)
    except ValueError as exc:
        raise DiffError(f"Invalid step reference: {ref}") from exc

    job = db_ctx.sessions.get_step_by_number(int(session["id"]), step_number, job_type)
    if not job:
        raise DiffError(f"Step not found: {ref}")

    outputs = db_ctx.jobs.get_outputs(job["id"])
    if not outputs:
        raise DiffError(f"Step {ref} has no output artifacts.")

    return str(outputs[0]["artifact_id"]), outputs[0].get("path")


def resolve_path_to_artifact(
    db_ctx: QueryDatabaseContext,
    ref: str,
    cwd: Path,
) -> tuple[str, str | None]:
    """Resolve a local filesystem path reference to the tracked artifact."""
    path_obj = Path(os.path.expanduser(ref))
    if not path_obj.is_absolute():
        path_obj = cwd / path_obj
    resolved_path = os.path.normpath(str(path_obj.absolute()))
    artifact = db_ctx.artifacts.get_by_path(resolved_path)
    if not artifact:
        raise DiffError(f"No artifact found for path: {ref}")
    return str(artifact["id"]), resolved_path


def resolve_session_ref(db_ctx: QueryDatabaseContext, ref: str) -> dict[str, Any]:
    """Resolve a ``session:`` reference to a session row dict."""
    session_key = ref[len(_SESSION_PREFIX) :]
    if session_key == "current":
        session = db_ctx.sessions.get_active()
        if not session:
            raise DiffError("No active session.")
        return session

    row = db_ctx._fetchone(
        "SELECT * FROM sessions WHERE hash LIKE ? LIMIT 2",
        (f"{session_key}%",),
    )
    if row is None:
        raise DiffError(f"Session not found: {session_key}")

    return {
        "id": int(row["id"]),
        "hash": row["hash"],
        "created_at": row["created_at"],
        "git_repo": row["git_repo"],
        "git_commit_start": row["git_commit_start"],
        "git_commit_end": row["git_commit_end"],
    }
