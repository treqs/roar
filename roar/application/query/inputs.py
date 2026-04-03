"""Application orchestration for the local inputs query."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ...db.query_context import create_query_database_context
from .requests import InputsQueryRequest
from .results import InputArtifactSummary, InputsSummary, ShowHashSummary

_NO_ACTIVE_SESSION_MESSAGE = "No active session. Run 'roar run' to create a session first."


class InputsQueryError(RuntimeError):
    """Raised when an inputs query cannot build a summary."""


def render_inputs(request: InputsQueryRequest) -> str:
    """Render root input artifacts for a target."""
    summary = build_inputs_summary(request)

    if request.output_json:
        return json.dumps(summary.to_dict(), indent=2)

    if summary.message:
        return summary.message

    lines: list[str] = []
    for art in summary.artifacts:
        digest = _short_digest(art.hashes)
        path = art.path or "(no path)"
        if digest:
            lines.append(f"{path}  {digest}")
        else:
            lines.append(path)
    return "\n".join(lines)


def _short_digest(hashes: list[ShowHashSummary]) -> str:
    """Return a short display digest like 'blake3:abcdef01...'."""
    for h in hashes:
        if h.algorithm == "blake3":
            return f"blake3:{h.digest[:12]}..."
    if hashes:
        return f"{hashes[0].algorithm}:{hashes[0].digest[:12]}..."
    return ""


def build_inputs_summary(request: InputsQueryRequest) -> InputsSummary:
    """Build a typed inputs summary for a target artifact or job."""
    with create_query_database_context(request.roar_dir) as db_ctx:
        target_artifact_ids, target_label = _resolve_target(
            db_ctx, request.cwd, request.ref, request.selector
        )

        if not target_artifact_ids:
            raise InputsQueryError(f'"{request.ref}" is not a tracked artifact')

        # Check if the target itself is a root (no producer)
        if len(target_artifact_ids) == 1 and _is_root_artifact(db_ctx, target_artifact_ids[0]):
            return InputsSummary(
                target_ref=target_label,
                is_root=True,
                message=f'"{target_label}" is a root artifact (no upstream inputs)',
            )

        if request.direct:
            artifacts = _get_direct_inputs(db_ctx, target_artifact_ids)
        elif request.show_all:
            artifacts = _walk_upstream_all(
                db_ctx, target_artifact_ids, exclude=set(target_artifact_ids)
            )
        else:
            artifacts = _walk_upstream_roots(db_ctx, target_artifact_ids)

        return InputsSummary(
            target_ref=target_label,
            is_root=False,
            artifacts=artifacts,
        )


def _resolve_target(db_ctx, cwd: Path, ref: str, selector: str) -> tuple[list[str], str]:
    """Resolve a reference to a list of starting artifact IDs and a display label.

    For artifact refs, returns [artifact_id] for the artifact itself.
    For job refs, returns the artifact IDs of the job's input artifacts.
    """
    if selector == "job":
        return _resolve_job_target(db_ctx, ref)
    if selector == "path":
        return _resolve_path_target(db_ctx, cwd, ref)
    if selector == "artifact":
        return _resolve_hash_target(db_ctx, ref)

    # Auto-detect
    ref_type = _classify_ref(ref, cwd)

    if ref_type == "job_step":
        return _resolve_job_target(db_ctx, ref)
    if ref_type == "file_path":
        return _resolve_path_target(db_ctx, cwd, ref)
    if ref_type == "job_uid":
        # Could be a job UID or short artifact hash -- try job first
        return _resolve_job_target(db_ctx, ref)
    if ref_type == "artifact_hash":
        return _resolve_hash_target(db_ctx, ref)

    # path_candidate: try as artifact path
    return _resolve_path_target(db_ctx, cwd, ref)


def _resolve_job_target(db_ctx, ref: str) -> tuple[list[str], str]:
    """Resolve a job ref to its input artifact IDs."""
    session = db_ctx.sessions.get_active()
    if not session:
        raise InputsQueryError(_NO_ACTIVE_SESSION_MESSAGE)

    if ref.startswith("@"):
        job = _resolve_job_ref(db_ctx, int(session["id"]), ref)
    else:
        job = db_ctx.jobs.get_by_uid(ref)

    if not job:
        raise InputsQueryError(f"Job not found: {ref}")

    inputs = db_ctx.jobs.get_inputs(job["id"])
    if not inputs:
        raise InputsQueryError(f"Job {ref} has no input artifacts")

    artifact_ids = [inp["artifact_id"] for inp in inputs]
    return artifact_ids, ref


def _resolve_path_target(db_ctx, cwd: Path, ref: str) -> tuple[list[str], str]:
    """Resolve a file path to its artifact ID."""
    artifact = _lookup_artifact_by_path(db_ctx, cwd, ref)
    if not artifact:
        raise InputsQueryError(f'"{ref}" is not a tracked artifact')
    return [artifact["id"]], ref


def _resolve_hash_target(db_ctx, ref: str) -> tuple[list[str], str]:
    """Resolve a hash prefix to its artifact ID."""
    artifact = db_ctx.artifacts.get_by_hash(ref)
    if not artifact:
        raise InputsQueryError(f'"{ref}" is not a tracked artifact')
    return [artifact["id"]], ref


def _classify_ref(ref: str, cwd: Path) -> str:
    """Classify a reference string by type."""
    if ref.startswith("@"):
        return "job_step"
    if "/" in ref or ref.startswith(("./", "../", "~")):
        return "file_path"
    is_hex = bool(ref) and all(c in "0123456789abcdefABCDEF" for c in ref)
    if is_hex and len(ref) <= 8:
        return "job_uid"
    if is_hex and len(ref) > 8:
        return "artifact_hash"
    return "path_candidate"


def _resolve_job_ref(db_ctx, session_id: int, job_ref: str) -> dict | None:
    """Resolve @N or @BN to a job dict."""
    if not job_ref.startswith("@"):
        return None
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


def _lookup_artifact_by_path(db_ctx, cwd: Path, ref: str) -> dict[str, Any] | None:
    """Look up an artifact by file path."""
    path_obj = Path(os.path.expanduser(ref))
    if not path_obj.is_absolute():
        path_obj = cwd / path_obj
    resolved_path = os.path.normpath(str(path_obj.absolute()))
    return db_ctx.artifacts.get_by_path(resolved_path)


def _is_root_artifact(db_ctx, artifact_id: str) -> bool:
    """Check if an artifact is a root — no producer, or producer has no inputs."""
    jobs = db_ctx.artifacts.get_jobs(artifact_id)
    produced_by = jobs.get("produced_by", [])
    if not produced_by:
        return True
    producer = produced_by[0]
    inputs = db_ctx.jobs.get_inputs(producer["id"])
    return len(inputs) == 0


def _make_artifact_summary(artifact: dict[str, Any]) -> InputArtifactSummary:
    """Build an InputArtifactSummary from a raw artifact dict."""
    return InputArtifactSummary(
        artifact_id=str(artifact["id"]),
        path=artifact.get("first_seen_path") or "",
        size=int(artifact.get("size", 0)),
        hashes=[
            ShowHashSummary(
                algorithm=str(h["algorithm"]),
                digest=str(h["digest"]),
            )
            for h in artifact.get("hashes", [])
        ],
    )


def _walk_upstream_roots(db_ctx, start_artifact_ids: list[str]) -> list[InputArtifactSummary]:
    """Walk the DAG upstream and collect only root artifacts (no producer)."""
    roots: dict[str, InputArtifactSummary] = {}
    visited: set[str] = set()
    path_stack: set[str] = set()

    def _trace(artifact_id: str) -> None:
        if artifact_id in visited:
            if artifact_id in path_stack:
                raise InputsQueryError("Circular dependency detected in artifact lineage")
            return
        visited.add(artifact_id)
        path_stack.add(artifact_id)

        jobs = db_ctx.artifacts.get_jobs(artifact_id)
        produced_by = jobs.get("produced_by", [])

        if not produced_by:
            # No producer at all — this is a root
            artifact = db_ctx.artifacts.get(artifact_id)
            if artifact:
                roots[artifact_id] = _make_artifact_summary(artifact)
        else:
            producer = produced_by[0]
            inputs = db_ctx.jobs.get_inputs(producer["id"])
            if not inputs:
                # Producer has no inputs (e.g., roar get) — treat this artifact as a root
                artifact = db_ctx.artifacts.get(artifact_id)
                if artifact:
                    roots[artifact_id] = _make_artifact_summary(artifact)
            else:
                for inp in inputs:
                    _trace(inp["artifact_id"])

        path_stack.discard(artifact_id)

    for aid in start_artifact_ids:
        _trace(aid)

    return list(roots.values())


def _get_direct_inputs(db_ctx, start_artifact_ids: list[str]) -> list[InputArtifactSummary]:
    """Get only the immediate inputs (one level up)."""
    seen: dict[str, InputArtifactSummary] = {}

    for artifact_id in start_artifact_ids:
        jobs = db_ctx.artifacts.get_jobs(artifact_id)
        produced_by = jobs.get("produced_by", [])
        if produced_by:
            producer = produced_by[0]
            inputs = db_ctx.jobs.get_inputs(producer["id"])
            for inp in inputs:
                aid = inp["artifact_id"]
                if aid not in seen:
                    artifact = db_ctx.artifacts.get(aid)
                    if artifact:
                        seen[aid] = _make_artifact_summary(artifact)

    return list(seen.values())


def _walk_upstream_all(
    db_ctx, start_artifact_ids: list[str], *, exclude: set[str] | None = None
) -> list[InputArtifactSummary]:
    """Walk the DAG upstream and collect all artifacts (intermediates + roots)."""
    exclude = exclude or set()
    collected: dict[str, InputArtifactSummary] = {}
    visited: set[str] = set()
    path_stack: set[str] = set()

    def _trace(artifact_id: str) -> None:
        if artifact_id in visited:
            if artifact_id in path_stack:
                raise InputsQueryError("Circular dependency detected in artifact lineage")
            return
        visited.add(artifact_id)
        path_stack.add(artifact_id)

        if artifact_id not in exclude:
            artifact = db_ctx.artifacts.get(artifact_id)
            if artifact:
                collected[artifact_id] = _make_artifact_summary(artifact)

        jobs = db_ctx.artifacts.get_jobs(artifact_id)
        produced_by = jobs.get("produced_by", [])
        if produced_by:
            producer = produced_by[0]
            inputs = db_ctx.jobs.get_inputs(producer["id"])
            for inp in inputs:
                _trace(inp["artifact_id"])

        path_stack.discard(artifact_id)

    for aid in start_artifact_ids:
        _trace(aid)

    return list(collected.values())
