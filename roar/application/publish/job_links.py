"""Pure helpers for local and GLaaS put-job artifact linking."""

from __future__ import annotations

from typing import Any

from ...application.publish.registration import normalize_registration_hashes
from ...integrations.glaas.registration import _artifact_ref
from .results import PutUploadedFile


def collect_local_composite_outputs(
    composite_registrations: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Collect local composite outputs to attach to the put job."""
    seen: dict[tuple[str, str], None] = {}
    for composite in composite_registrations:
        local_artifact_id = composite.get("local_artifact_id")
        root_path = composite.get("root_path")
        if (
            isinstance(local_artifact_id, str)
            and local_artifact_id
            and isinstance(root_path, str)
            and root_path
        ):
            seen[(local_artifact_id, root_path)] = None
    return list(seen)


def collect_local_lineage_composite_inputs(
    lineage_artifacts: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Collect upstream local composite inputs to attach to the put job."""
    seen: dict[tuple[str, str], None] = {}
    for artifact in lineage_artifacts:
        if artifact.get("kind") != "composite":
            continue
        artifact_id = artifact.get("id")
        path = _artifact_ref.artifact_path(artifact)
        if isinstance(artifact_id, str) and artifact_id and path:
            seen[(artifact_id, path)] = None
    return list(seen)


def build_put_job_link_outputs(
    composite_registrations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build GLaaS output link payloads for registered composite outputs."""
    return [
        {
            "artifact_hash": registration["hash"],
            "path": registration["root_path"],
        }
        for registration in composite_registrations
        if registration.get("registered") and registration.get("hash")
    ]


def build_put_job_link_inputs(
    *,
    coordinator: Any,
    uploaded_files: list[PutUploadedFile],
    lineage_artifacts: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Resolve GLaaS input link payloads for uploaded files and lineage composites."""
    candidates: list[dict[str, Any]] = []

    for uploaded in uploaded_files:
        digest = uploaded.hash
        path = uploaded.local_path
        if isinstance(digest, str) and digest and isinstance(path, str) and path:
            candidates.append({"hash": digest, "path": path})

    for artifact in lineage_artifacts:
        if artifact.get("kind") != "composite":
            continue

        artifact_path = _artifact_ref.artifact_path(artifact)
        if not isinstance(artifact_path, str) or not artifact_path:
            continue

        hashes = normalize_registration_hashes(artifact)
        if not hashes:
            continue

        candidates.append({"hashes": hashes, "path": artifact_path})

    return resolve_put_job_link_input_candidates(coordinator=coordinator, candidates=candidates)


def resolve_put_job_link_input_candidates(
    *,
    coordinator: Any,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Resolve candidate artifacts into deduplicated GLaaS job-input links."""
    artifact_service = coordinator.artifact_service

    resolved_inputs: list[dict[str, str]] = []
    errors: list[str] = []
    artifact_hash_cache: dict[str, str] = {}
    seen_rows: set[tuple[str, str]] = set()

    for candidate in candidates:
        path = candidate.get("path")
        if not isinstance(path, str) or not path:
            continue

        cache_key = _artifact_ref.cache_key(candidate)
        artifact_hash = artifact_hash_cache.get(cache_key) if cache_key else None
        if not artifact_hash:
            resolution = artifact_service.resolve_artifact_hash(candidate)
            if not isinstance(resolution, tuple) or len(resolution) != 2:
                continue
            resolved_artifact_hash, error = resolution
            if error:
                errors.append(
                    f"{(_artifact_ref.preview(candidate) or 'unknown-artifact')} could not be resolved: {error}"
                )
                continue
            if not isinstance(resolved_artifact_hash, str) or not resolved_artifact_hash:
                errors.append(
                    f"{(_artifact_ref.preview(candidate) or 'unknown-artifact')} returned empty artifact hash"
                )
                continue
            artifact_hash = resolved_artifact_hash
            if cache_key:
                artifact_hash_cache[cache_key] = artifact_hash

        row_key = (artifact_hash, path)
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)
        resolved_inputs.append({"artifact_hash": artifact_hash, "path": path})

    return resolved_inputs, errors
