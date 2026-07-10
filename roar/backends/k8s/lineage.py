"""Kubernetes fragment shaping for the shared fragment lineage engine."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from roar.execution.fragments.lineage import (
    FragmentLineageBackend,
    merge_execution_fragments,
)
from roar.execution.fragments.models import (
    ExecutionFragment,
    derive_fragment_identity,
)


def _k8s_fragment_command(fragment: ExecutionFragment) -> str:
    return f"k8s_task:{fragment.task_name or 'task'}"


def _k8s_fragment_metadata(
    fragment: ExecutionFragment,
    fallback_parent_job_uid: str | None,
) -> Mapping[str, Any] | None:
    metadata: dict[str, Any] = {
        "k8s_task_id": fragment.task_id,
        "parent_job_uid": fragment.parent_job_uid or fallback_parent_job_uid or None,
    }
    for key, value in (fragment.backend_metadata or {}).items():
        if value is not None:
            metadata[key] = value
    return metadata


K8S_FRAGMENT_LINEAGE_BACKEND = FragmentLineageBackend(
    job_type="k8s_task",
    command_for_fragment=_k8s_fragment_command,
    script_for_fragment=lambda fragment: fragment.task_name or None,
    execution_role_from_fragment=lambda fragment, _fallback: str(
        (fragment.backend_metadata or {}).get("execution_role") or "task"
    ),
    metadata_from_fragment=_k8s_fragment_metadata,
    task_identity_from_metadata=lambda parent_job_uid, job_uid, metadata: derive_fragment_identity(
        "k8s",
        parent_job_uid,
        str(metadata.get("k8s_task_id") or metadata.get("task_id") or ""),
        job_uid,
    ),
)


def collect_k8s_fragments(
    fragments: list[dict[str, Any]],
    *,
    project_dir: str,
    driver_job_uid: str | None = None,
    session_id: int | None = None,
    step_number: int = 1,
) -> int:
    """Merge k8s fragment dicts into the local DB; returns fragments merged."""
    parsed: list[ExecutionFragment] = []
    for payload in fragments:
        if not isinstance(payload, dict):
            continue
        try:
            parsed.append(ExecutionFragment.from_dict(payload))
        except Exception:
            continue

    if not parsed:
        return 0

    merge_execution_fragments(
        fragments=parsed,
        project_dir=project_dir,
        backend=K8S_FRAGMENT_LINEAGE_BACKEND,
        driver_job_uid=driver_job_uid,
        session_id=session_id,
        step_number=step_number,
    )
    return len(parsed)


def resolve_active_session_context(db_path: str) -> tuple[int | None, int]:
    """Return ``(active_session_id, next_step_number)`` for fragment merges."""
    if not db_path or not os.path.exists(db_path):
        return None, 1

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None, 1

    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id FROM sessions WHERE is_active = 1 LIMIT 1").fetchone()
        if row is None:
            return None, 1
        session_id = int(row["id"])
        step_row = conn.execute(
            "SELECT COALESCE(MAX(step_number), 0) AS max_step FROM jobs WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        max_step = int(step_row["max_step"]) if step_row is not None else 0
        return session_id, max_step + 1
    except sqlite3.Error:
        return None, 1
    finally:
        conn.close()


__all__ = [
    "K8S_FRAGMENT_LINEAGE_BACKEND",
    "collect_k8s_fragments",
    "resolve_active_session_context",
]
