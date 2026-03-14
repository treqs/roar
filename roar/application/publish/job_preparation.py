"""Pure registration-preparation helpers for local lineage jobs."""

from __future__ import annotations

from typing import Any

from ...application.publish.registration import normalize_registration_hashes
from ...execution.framework.registry import (
    is_execution_noise_job,
    is_execution_submit_job,
    is_execution_task_job,
)
from ...integrations.glaas.registration import _artifact_ref


def normalize_jobs_for_registration(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop known noise jobs and repair unresolved local parent references."""
    normalized = [dict(job) for job in jobs if not is_execution_noise_job(job)]
    known_job_uids = {
        str(job["job_uid"]) for job in normalized if isinstance(job.get("job_uid"), str)
    }
    root_candidates = [job for job in normalized if _is_local_parent_candidate(job)]
    if not root_candidates:
        root_candidates = [job for job in normalized if not is_execution_task_job(job)]

    for job in normalized:
        parent_uid = str(job.get("parent_job_uid") or "").strip()
        if not parent_uid or parent_uid in known_job_uids:
            continue

        inferred_parent_uid = _infer_local_parent_uid(job, root_candidates)
        if inferred_parent_uid:
            job["parent_job_uid"] = inferred_parent_uid
        else:
            job["parent_job_uid"] = None

    return normalized


def order_jobs_for_registration(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order jobs so parents are registered before their children."""
    jobs_by_uid = {
        str(job["job_uid"]): job for job in jobs if isinstance(job.get("job_uid"), str)
    }
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(job: dict[str, Any]) -> None:
        parent_uid = job.get("parent_job_uid")
        if isinstance(parent_uid, str) and parent_uid:
            parent = jobs_by_uid.get(parent_uid)
            if parent is not None:
                visit(parent)

        visit_key = str(job.get("job_uid") or f"id:{job.get('id')}")
        if visit_key in seen:
            return
        seen.add(visit_key)
        ordered.append(job)

    for job in sorted(
        jobs,
        key=lambda item: (
            int(item.get("step_number") or 0),
            float(item.get("timestamp") or 0.0),
            int(item.get("id") or 0),
        ),
    ):
        visit(job)

    return ordered


def estimate_links(jobs: list[dict[str, Any]]) -> int:
    """Estimate number of artifact links represented by the lineage jobs."""
    links = 0
    for job in jobs:
        links += len(job.get("_inputs", []))
        links += len(job.get("_outputs", []))
    return links


def refresh_job_artifact_references(
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> None:
    """Replace weak path/hash refs on job IO edges with preferred registration digests."""
    preferred_hash_by_path: dict[str, str] = {}
    preferred_hash_by_digest: dict[str, str] = {}

    for artifact in artifacts:
        preferred_hash = _select_preferred_link_hash(artifact)
        if preferred_hash is None:
            continue

        artifact_path = _artifact_ref.artifact_path(artifact)
        if artifact_path:
            preferred_hash_by_path[artifact_path] = preferred_hash

        for entry in normalize_registration_hashes(artifact, fallback_to_hash=True):
            digest = entry.get("digest")
            if isinstance(digest, str) and digest:
                preferred_hash_by_digest[digest] = preferred_hash

    for job in jobs:
        _refresh_job_io_refs(
            job, "_inputs", "_input_hashes", preferred_hash_by_path, preferred_hash_by_digest
        )
        _refresh_job_io_refs(
            job, "_outputs", "_output_hashes", preferred_hash_by_path, preferred_hash_by_digest
        )


def _infer_local_parent_uid(
    job: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str | None:
    job_step = int(job.get("step_number") or 0)
    job_timestamp = float(job.get("timestamp") or 0.0)

    eligible = [
        candidate
        for candidate in candidates
        if (
            int(candidate.get("step_number") or 0) < job_step
            or (
                int(candidate.get("step_number") or 0) == job_step
                and float(candidate.get("timestamp") or 0.0) <= job_timestamp
            )
        )
    ]
    if not eligible:
        return None

    preferred = max(eligible, key=_parent_candidate_sort_key)
    inferred_uid = preferred.get("job_uid")
    return str(inferred_uid) if inferred_uid else None


def _is_local_parent_candidate(job: dict[str, Any]) -> bool:
    job_type = str(job.get("job_type", "") or "")
    return not is_execution_task_job(job) and not is_execution_noise_job(job) and job_type != "build"


def _parent_candidate_sort_key(job: dict[str, Any]) -> tuple[int, int, float, int]:
    return (
        1 if is_execution_submit_job(job) else 0,
        int(job.get("step_number") or 0),
        float(job.get("timestamp") or 0.0),
        int(job.get("id") or 0),
    )


def _refresh_job_io_refs(
    job: dict[str, Any],
    items_key: str,
    hashes_key: str,
    preferred_hash_by_path: dict[str, str],
    preferred_hash_by_digest: dict[str, str],
) -> None:
    items = job.get(items_key)
    if not isinstance(items, list):
        return

    updated_hashes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        preferred_hash: str | None = None
        path = item.get("path")
        if isinstance(path, str) and path:
            preferred_hash = preferred_hash_by_path.get(path)

        raw_hash = item.get("hash")
        if preferred_hash is None and isinstance(raw_hash, str) and raw_hash:
            preferred_hash = preferred_hash_by_digest.get(raw_hash.lower())

        if preferred_hash is not None:
            item["hash"] = preferred_hash

        item_hash = item.get("hash")
        if isinstance(item_hash, str) and item_hash:
            updated_hashes.append(item_hash)

    job[hashes_key] = updated_hashes


def _select_preferred_link_hash(artifact: dict[str, Any]) -> str | None:
    hashes = normalize_registration_hashes(artifact, fallback_to_hash=True)
    for preferred_algorithm in ("blake3", "composite-blake3"):
        for entry in hashes:
            if entry.get("algorithm") == preferred_algorithm:
                digest = entry.get("digest")
                if isinstance(digest, str) and digest:
                    return digest
    if hashes:
        digest = hashes[0].get("digest")
        if isinstance(digest, str) and digest:
            return digest
    return None
