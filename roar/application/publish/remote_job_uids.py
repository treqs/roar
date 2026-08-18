"""Helpers for deriving publication-scoped remote job UIDs."""

from __future__ import annotations

import hashlib
from typing import Any


def build_remote_publication_job_uid(session_hash: str, local_job_uid: str) -> str:
    """Derive a deterministic remote job UID for one published lineage snapshot.

    Remote GLaaS jobs are still stored with a one-lineage/one-row model. Re-publishing
    the same local lineage after it grows must therefore avoid reusing the same
    ``job_uid`` values across distinct finalized remote lineages. The published lineage
    hash scopes the remote job identity without changing the local job UID stored in the
    `.roar` database.
    """
    normalized_session_hash = str(session_hash).strip().lower()
    normalized_local_job_uid = str(local_job_uid).strip()
    digest = hashlib.sha256(
        f"{normalized_session_hash}\0{normalized_local_job_uid}".encode()
    ).hexdigest()
    return digest[:32]


def prepare_jobs_for_remote_publication(
    jobs: list[dict[str, Any]],
    session_hash: str,
) -> list[dict[str, Any]]:
    """Return job payload copies annotated with publication-scoped remote job UIDs."""
    remote_uid_by_local_uid = {
        str(job_uid): build_remote_publication_job_uid(session_hash, str(job_uid))
        for job in jobs
        if isinstance((job_uid := job.get("job_uid")), str) and job_uid
    }

    prepared_jobs: list[dict[str, Any]] = []
    for job in jobs:
        prepared = dict(job)
        local_job_uid = prepared.get("job_uid")
        if isinstance(local_job_uid, str) and local_job_uid:
            prepared["remote_job_uid"] = remote_uid_by_local_uid[local_job_uid]

        parent_job_uid = prepared.get("parent_job_uid")
        if isinstance(parent_job_uid, str) and parent_job_uid:
            prepared["remote_parent_job_uid"] = remote_uid_by_local_uid.get(parent_job_uid)

        prepared_jobs.append(prepared)

    return prepared_jobs


def apply_remote_publication_job_uid_mapping(
    jobs: list[dict[str, Any]],
    remote_uid_by_local_uid: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply the authoritative mapping persisted by a completed publication."""
    prepared_jobs: list[dict[str, Any]] = []
    for job in jobs:
        prepared = dict(job)
        local_job_uid = prepared.get("job_uid")
        if isinstance(local_job_uid, str) and local_job_uid:
            remote_job_uid = remote_uid_by_local_uid.get(local_job_uid)
            if remote_job_uid:
                prepared["remote_job_uid"] = remote_job_uid

        parent_job_uid = prepared.get("parent_job_uid")
        if isinstance(parent_job_uid, str) and parent_job_uid:
            prepared["remote_parent_job_uid"] = remote_uid_by_local_uid.get(parent_job_uid)
        prepared_jobs.append(prepared)
    return prepared_jobs
