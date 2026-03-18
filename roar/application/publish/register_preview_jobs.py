"""Lightweight job normalization helpers for local register preview flows."""

from __future__ import annotations

from typing import Any

from ...db.step_priority import is_host_or_submit_job, is_noise_job, is_task_job


def normalize_jobs_for_registration(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop known noise jobs and repair unresolved local parent references."""
    normalized = [dict(job) for job in jobs if not is_noise_job(job)]
    known_job_uids = {
        str(job["job_uid"]) for job in normalized if isinstance(job.get("job_uid"), str)
    }
    root_candidates = [job for job in normalized if _is_local_parent_candidate(job)]
    if not root_candidates:
        root_candidates = [job for job in normalized if not is_task_job(job)]

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
    jobs_by_uid = {str(job["job_uid"]): job for job in jobs if isinstance(job.get("job_uid"), str)}
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
    return not is_task_job(job) and not is_noise_job(job) and job_type != "build"


def _parent_candidate_sort_key(job: dict[str, Any]) -> tuple[int, int, float, int]:
    return (
        1 if is_host_or_submit_job(job) else 0,
        int(job.get("step_number") or 0),
        float(job.get("timestamp") or 0.0),
        int(job.get("id") or 0),
    )
