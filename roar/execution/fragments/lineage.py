from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roar.execution.fragments.models import (
    ArtifactRef,
    ExecutionFragment,
    derive_fragment_identity,
    resolve_execution_fragment_identity,
)


@dataclass(frozen=True)
class FragmentLineageBackend:
    job_type: str
    command_for_fragment: Callable[[ExecutionFragment], str]
    script_for_fragment: Callable[[ExecutionFragment], str | None]
    execution_role_from_fragment: Callable[[ExecutionFragment, str | None], str | None]
    metadata_from_fragment: Callable[
        [ExecutionFragment, str | None],
        Mapping[str, Any] | None,
    ]
    task_identity_from_metadata: Callable[[str, str, Mapping[str, Any]], str]


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


def merge_execution_fragments(
    *,
    fragments: list[ExecutionFragment],
    project_dir: str,
    backend: FragmentLineageBackend,
    driver_job_uid: str | None = None,
    session_id: int | None = None,
    step_number: int = 1,
) -> None:
    db_path = os.path.join(project_dir, ".roar", "roar.db")
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    touched_job_ids: set[int] = set()
    committed = False
    try:
        artifact_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        now = time.time()
        if not fragments:
            conn.commit()
            return

        _resolve_fragment_job_uids(
            conn=conn,
            job_columns=job_columns,
            fragments=fragments,
            driver_job_uid=driver_job_uid,
            backend=backend,
        )
        step_by_job_uid = assign_execution_fragment_step_numbers(
            fragments,
            base_step=step_number,
        )

        for fragment in fragments:
            fragment_step_number = step_by_job_uid.get(fragment.job_uid, step_number + 1)

            _insert_fragment_job(
                conn=conn,
                job_columns=job_columns,
                fragment=fragment,
                backend=backend,
                driver_job_uid=driver_job_uid,
                session_id=session_id,
                step_number=fragment_step_number,
                now=now,
            )

            row = conn.execute(
                "SELECT id FROM jobs WHERE job_uid = ? ORDER BY id DESC LIMIT 1",
                (fragment.job_uid,),
            ).fetchone()
            if row is None:
                continue
            job_id = int(row["id"])
            touched_job_ids.add(job_id)

            written_artifact_ids: dict[str, str] = {}

            for ref in fragment.writes:
                artifact_id = _upsert_artifact_for_ref(
                    conn,
                    columns=artifact_columns,
                    ref=ref,
                    now=now,
                )
                written_artifact_ids[str(ref.path)] = artifact_id
                conn.execute(
                    """
                    INSERT OR IGNORE INTO job_outputs
                        (job_id, artifact_id, path)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, artifact_id, ref.path),
                )

            for ref in fragment.reads:
                artifact_id = written_artifact_ids.get(str(ref.path), "")
                if not artifact_id:
                    artifact_id = _upsert_artifact_for_ref(
                        conn,
                        columns=artifact_columns,
                        ref=ref,
                        now=now,
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO job_inputs
                        (job_id, artifact_id, path)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, artifact_id, ref.path),
                )

        conn.commit()
        committed = True
    finally:
        conn.close()

    if committed:
        _refresh_fragment_job_system_labels(project_dir=project_dir, job_ids=touched_job_ids)


def _refresh_fragment_job_system_labels(*, project_dir: str, job_ids: set[int]) -> None:
    if not job_ids:
        return

    try:
        from roar.application.system_labels import refresh_job_system_labels
        from roar.db.context import create_database_context
    except Exception:
        return

    try:
        with create_database_context(Path(project_dir) / ".roar") as db_ctx:
            for job_id in sorted(job_ids):
                refresh_job_system_labels(db_ctx, job_id=job_id)
    except Exception as exc:
        _get_logger().warning(
            "Failed to refresh fragment job system labels in %s: %s",
            project_dir,
            exc,
        )


def assign_execution_fragment_step_numbers(
    fragments: list[ExecutionFragment],
    base_step: int = 1,
) -> dict[str, int]:
    if not fragments:
        return {}

    job_index_by_uid: dict[str, int] = {}
    job_uids: list[str] = []
    reads_by_job: list[set[str]] = []
    writes_by_job: list[set[str]] = []
    for fragment in fragments:
        job_index = job_index_by_uid.get(fragment.job_uid)
        if job_index is None:
            job_index = len(job_uids)
            job_index_by_uid[fragment.job_uid] = job_index
            job_uids.append(fragment.job_uid)
            reads_by_job.append(set())
            writes_by_job.append(set())

        for ref in fragment.reads:
            reads_by_job[job_index].update(_dependency_tokens_for_ref(ref))

        for ref in fragment.writes:
            writes_by_job[job_index].update(_dependency_tokens_for_ref(ref))

    producers_by_hash: dict[str, set[int]] = {}
    for producer_index, writes in enumerate(writes_by_job):
        for hash_value in writes:
            producers_by_hash.setdefault(hash_value, set()).add(producer_index)

    adjacency: list[set[int]] = [set() for _ in job_uids]
    indegree = [0] * len(job_uids)
    depth = [1] * len(job_uids)

    for consumer_index, reads in enumerate(reads_by_job):
        for hash_value in reads:
            for producer_index in producers_by_hash.get(hash_value, set()):
                if producer_index == consumer_index:
                    continue
                if consumer_index in adjacency[producer_index]:
                    continue
                adjacency[producer_index].add(consumer_index)
                indegree[consumer_index] += 1

    queue = deque(index for index, item_indegree in enumerate(indegree) if item_indegree == 0)
    processed = 0
    while queue:
        producer_index = queue.popleft()
        processed += 1
        for consumer_index in adjacency[producer_index]:
            depth[consumer_index] = max(depth[consumer_index], depth[producer_index] + 1)
            indegree[consumer_index] -= 1
            if indegree[consumer_index] == 0:
                queue.append(consumer_index)

    if processed != len(job_uids):
        fallback_depth = (max(depth) if depth else 1) + 1
        for index, item_indegree in enumerate(indegree):
            if item_indegree > 0:
                depth[index] = fallback_depth

    return {job_uid: base_step + depth[index] for index, job_uid in enumerate(job_uids)}


def _resolve_fragment_job_uids(
    *,
    conn: sqlite3.Connection,
    job_columns: set[str],
    fragments: list[ExecutionFragment],
    driver_job_uid: str | None,
    backend: FragmentLineageBackend,
) -> None:
    task_identity_by_fragment: dict[int, str] = {
        index: resolve_execution_fragment_identity(
            fragment,
            fallback_parent_job_uid=driver_job_uid,
        )
        for index, fragment in enumerate(fragments)
    }
    claimed_identity_by_job_uid, existing_job_uid_by_identity = _load_existing_job_identity_claims(
        conn,
        job_columns=job_columns,
        job_uids=[fragment.job_uid for fragment in fragments],
        task_identities=list(task_identity_by_fragment.values()),
        backend=backend,
    )
    resolved_job_uid_by_identity: dict[str, str] = dict(existing_job_uid_by_identity)

    for index, fragment in enumerate(fragments):
        task_identity = task_identity_by_fragment[index]
        resolution_key = task_identity or f"fragment:{index}"
        preferred_job_uid = str(fragment.job_uid or "").strip()
        if resolution_key in resolved_job_uid_by_identity:
            fragment.job_uid = resolved_job_uid_by_identity[resolution_key]
            fragment.task_identity = task_identity
            continue

        if not preferred_job_uid:
            preferred_job_uid = task_identity or str(uuid.uuid4())

        claimed_identity = claimed_identity_by_job_uid.get(preferred_job_uid)
        resolved_job_uid = preferred_job_uid
        if claimed_identity and claimed_identity != task_identity:
            resolved_job_uid = task_identity or preferred_job_uid

        fragment.job_uid = resolved_job_uid
        fragment.task_identity = task_identity or fragment.task_identity
        resolved_job_uid_by_identity[resolution_key] = fragment.job_uid
        if fragment.task_identity:
            claimed_identity_by_job_uid[fragment.job_uid] = fragment.task_identity


def _load_existing_job_identity_claims(
    conn: sqlite3.Connection,
    *,
    job_columns: set[str],
    job_uids: list[str],
    task_identities: list[str],
    backend: FragmentLineageBackend,
) -> tuple[dict[str, str], dict[str, str]]:
    claims: dict[str, str] = {}
    job_uid_by_identity: dict[str, str] = {}
    select_fields = ["job_uid"]
    if "parent_job_uid" in job_columns:
        select_fields.append("parent_job_uid")
    if "metadata" in job_columns:
        select_fields.append("metadata")

    for job_uid in {str(item or "").strip() for item in job_uids if str(item or "").strip()}:
        row = conn.execute(
            f"SELECT {', '.join(select_fields)} FROM jobs WHERE job_uid = ? ORDER BY id DESC LIMIT 1",
            (job_uid,),
        ).fetchone()
        if row is None:
            continue
        task_identity = _job_row_task_identity(row, backend=backend)
        if task_identity:
            claims[job_uid] = task_identity
            job_uid_by_identity.setdefault(task_identity, job_uid)

    unresolved_task_identities = {
        str(item or "").strip()
        for item in task_identities
        if str(item or "").strip() and str(item or "").strip() not in job_uid_by_identity
    }
    if unresolved_task_identities:
        rows = conn.execute(
            f"SELECT {', '.join(select_fields)} FROM jobs ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            task_identity = _job_row_task_identity(row, backend=backend)
            if task_identity not in unresolved_task_identities:
                continue
            resolved_job_uid = str(row["job_uid"] or "").strip()
            if not resolved_job_uid:
                continue
            claims.setdefault(resolved_job_uid, task_identity)
            job_uid_by_identity[task_identity] = resolved_job_uid
            unresolved_task_identities.discard(task_identity)
            if not unresolved_task_identities:
                break

    return claims, job_uid_by_identity


def _job_row_task_identity(
    row: sqlite3.Row,
    *,
    backend: FragmentLineageBackend,
) -> str:
    row_keys = set(row.keys())
    metadata = _safe_row_metadata(row)
    task_identity = str(metadata.get("task_identity") or "").strip()
    if task_identity:
        return task_identity

    parent_job_uid = str(row["parent_job_uid"] or "") if "parent_job_uid" in row_keys else ""
    generic_backend = str(metadata.get("backend") or "").strip()
    generic_task_id = str(metadata.get("task_id") or "").strip()
    generic_identity = derive_fragment_identity(
        generic_backend,
        parent_job_uid,
        generic_task_id,
        str(row["job_uid"] or ""),
    )
    if generic_identity:
        return generic_identity

    return backend.task_identity_from_metadata(
        parent_job_uid,
        str(row["job_uid"] or ""),
        metadata,
    )


def _safe_row_metadata(row: sqlite3.Row) -> dict[str, Any]:
    if "metadata" not in set(row.keys()):
        return {}
    raw = row["metadata"]
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _insert_fragment_job(
    conn: sqlite3.Connection,
    *,
    job_columns: set[str],
    fragment: ExecutionFragment,
    backend: FragmentLineageBackend,
    driver_job_uid: str | None,
    session_id: int | None,
    step_number: int,
    now: float,
) -> None:
    command = backend.command_for_fragment(fragment)
    script = backend.script_for_fragment(fragment)
    timestamp = fragment.started_at or now
    duration = max(0.0, float(fragment.ended_at - fragment.started_at))
    parent_job_uid = fragment.parent_job_uid or driver_job_uid

    fields = [
        "job_uid",
        "timestamp",
        "command",
        "script",
        "duration_seconds",
        "exit_code",
        "status",
        "job_type",
    ]
    values: list[Any] = [
        fragment.job_uid,
        timestamp,
        command,
        script,
        duration,
        fragment.exit_code,
        "completed",
        backend.job_type,
    ]

    if "parent_job_uid" in job_columns:
        fields.append("parent_job_uid")
        values.append(parent_job_uid)
    if "execution_backend" in job_columns:
        fields.append("execution_backend")
        values.append(fragment.backend)
    if "execution_role" in job_columns:
        fields.append("execution_role")
        values.append(backend.execution_role_from_fragment(fragment, driver_job_uid))
    if "session_id" in job_columns and session_id is not None:
        fields.append("session_id")
        values.append(session_id)
    if "step_number" in job_columns:
        fields.append("step_number")
        values.append(step_number)
    if "metadata" in job_columns:
        fields.append("metadata")
        values.append(
            json.dumps(
                _build_fragment_job_metadata(
                    fragment,
                    backend=backend,
                    fallback_parent_job_uid=driver_job_uid,
                )
            )
        )

    placeholders = ", ".join("?" for _ in fields)
    field_list = ", ".join(fields)
    conn.execute(
        f"INSERT OR IGNORE INTO jobs ({field_list}) VALUES ({placeholders})",
        values,
    )
    _update_fragment_job(
        conn=conn,
        job_columns=job_columns,
        fragment=fragment,
        backend=backend,
        parent_job_uid=parent_job_uid,
        session_id=session_id,
        step_number=step_number,
        timestamp=timestamp,
        duration=duration,
        command=command,
        script=script,
    )


def _update_fragment_job(
    conn: sqlite3.Connection,
    *,
    job_columns: set[str],
    fragment: ExecutionFragment,
    backend: FragmentLineageBackend,
    parent_job_uid: str | None,
    session_id: int | None,
    step_number: int,
    timestamp: float,
    duration: float,
    command: str,
    script: str | None,
) -> None:
    metadata_json = ""
    if "metadata" in job_columns:
        metadata_json = json.dumps(
            _build_fragment_job_metadata(
                fragment,
                backend=backend,
                fallback_parent_job_uid=parent_job_uid,
            )
        )

    updates = [
        "timestamp = CASE WHEN timestamp > ? THEN ? ELSE timestamp END",
        "duration_seconds = CASE WHEN duration_seconds IS NULL OR duration_seconds < ? THEN ? ELSE duration_seconds END",
        "exit_code = CASE WHEN ? != 0 THEN ? ELSE COALESCE(exit_code, 0) END",
        "command = CASE WHEN command IS NULL OR command = '' THEN ? ELSE command END",
        "script = CASE WHEN script IS NULL OR script = '' THEN ? ELSE script END",
        "status = COALESCE(status, 'completed')",
        f"job_type = COALESCE(job_type, '{backend.job_type}')",
    ]
    params: list[Any] = [
        timestamp,
        timestamp,
        duration,
        duration,
        fragment.exit_code,
        fragment.exit_code,
        command,
        script,
    ]

    if "parent_job_uid" in job_columns:
        updates.append(
            "parent_job_uid = CASE WHEN (parent_job_uid IS NULL OR parent_job_uid = '') AND ? IS NOT NULL AND ? != '' THEN ? ELSE parent_job_uid END"
        )
        params.extend([parent_job_uid, parent_job_uid, parent_job_uid])
    if "execution_backend" in job_columns:
        updates.append("execution_backend = COALESCE(execution_backend, ?)")
        params.append(fragment.backend)
    if "execution_role" in job_columns:
        execution_role = backend.execution_role_from_fragment(fragment, parent_job_uid)
        updates.append("execution_role = COALESCE(execution_role, ?)")
        params.append(execution_role)
    if "session_id" in job_columns and session_id is not None:
        updates.append("session_id = COALESCE(session_id, ?)")
        params.append(session_id)
    if "step_number" in job_columns:
        updates.append(
            "step_number = CASE WHEN step_number IS NULL OR step_number < ? THEN ? ELSE step_number END"
        )
        params.extend([step_number, step_number])
    if "metadata" in job_columns:
        updates.append(
            "metadata = CASE WHEN (metadata IS NULL OR metadata = '') AND ? != '' THEN ? ELSE metadata END"
        )
        params.extend([metadata_json, metadata_json])

    params.append(fragment.job_uid)
    conn.execute(
        f"UPDATE jobs SET {', '.join(updates)} WHERE job_uid = ?",
        params,
    )


def _build_fragment_job_metadata(
    fragment: ExecutionFragment,
    *,
    backend: FragmentLineageBackend,
    fallback_parent_job_uid: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "task_identity": resolve_execution_fragment_identity(
            fragment,
            fallback_parent_job_uid=fallback_parent_job_uid,
        ),
        "backend": fragment.backend,
        "task_id": fragment.task_id,
        "worker_id": fragment.worker_id,
        "node_id": fragment.node_id,
        "task_name": fragment.task_name,
    }
    if fragment.actor_id:
        metadata["actor_id"] = fragment.actor_id

    backend_metadata = dict(backend.metadata_from_fragment(fragment, fallback_parent_job_uid) or {})
    metadata.update(backend_metadata)
    return metadata


def _upsert_artifact_for_ref(
    conn: sqlite3.Connection,
    *,
    columns: set[str],
    ref: ArtifactRef,
    now: float,
) -> str:
    digest = _normalize_hash(_to_text(ref.hash))
    algorithm = _to_text(ref.hash_algorithm)
    existing_by_path = conn.execute(
        "SELECT id, hash FROM artifacts WHERE first_seen_path = ? ORDER BY first_seen_at DESC LIMIT 1",
        (ref.path,),
    ).fetchone()

    if digest and algorithm:
        existing_by_hash = conn.execute(
            """
            SELECT artifact_id
            FROM artifact_hashes
            WHERE algorithm = ? AND digest = ?
            LIMIT 1
            """,
            (algorithm, digest),
        ).fetchone()
        if existing_by_path is not None:
            path_artifact_id = str(existing_by_path["id"])
            path_digest_row = conn.execute(
                """
                SELECT digest
                FROM artifact_hashes
                WHERE artifact_id = ? AND algorithm = ?
                LIMIT 1
                """,
                (path_artifact_id, algorithm),
            ).fetchone()
            path_digest = _normalize_hash(
                _to_text(path_digest_row["digest"])
                if path_digest_row is not None
                else existing_by_path["hash"]
            )

            if path_digest in (None, digest):
                _backfill_artifact_for_ref(
                    conn,
                    columns=columns,
                    artifact_id=path_artifact_id,
                    ref=ref,
                    digest=digest,
                    algorithm=algorithm,
                )
                return path_artifact_id

        if existing_by_hash is not None:
            return str(existing_by_hash["artifact_id"])

    if not digest and existing_by_path is not None:
        return str(existing_by_path["id"])

    artifact_id = str(uuid.uuid4())
    metadata_payload = {"capture_method": ref.capture_method}
    _insert_artifact(
        conn,
        columns=columns,
        artifact_id=artifact_id,
        now=now,
        path=ref.path,
        source_type=_normalize_source_type(None, ref.path),
        capture_method=_normalize_capture_method(ref.capture_method),
        hash_value=digest,
        size=ref.size,
        metadata=json.dumps(metadata_payload),
    )

    if digest and algorithm:
        conn.execute(
            """
            INSERT OR IGNORE INTO artifact_hashes
                (artifact_id, algorithm, digest)
            VALUES (?, ?, ?)
            """,
            (artifact_id, algorithm, digest),
        )
        existing = conn.execute(
            """
            SELECT artifact_id
            FROM artifact_hashes
            WHERE algorithm = ? AND digest = ?
            LIMIT 1
            """,
            (algorithm, digest),
        ).fetchone()
        if existing is not None:
            return str(existing["artifact_id"])

    return artifact_id


def _backfill_artifact_for_ref(
    conn: sqlite3.Connection,
    *,
    columns: set[str],
    artifact_id: str,
    ref: ArtifactRef,
    digest: str,
    algorithm: str,
) -> None:
    updates: list[str] = []
    params: list[Any] = []

    if "hash" in columns:
        updates.append("hash = COALESCE(NULLIF(hash, ''), ?)")
        params.append(digest)
    if "capture_method" in columns:
        updates.append("capture_method = COALESCE(NULLIF(capture_method, ''), ?)")
        params.append(_normalize_capture_method(ref.capture_method))
    if ref.size > 0:
        updates.append("size = CASE WHEN size <= 0 THEN ? ELSE size END")
        params.append(ref.size)

    if updates:
        params.append(artifact_id)
        conn.execute(
            f"UPDATE artifacts SET {', '.join(updates)} WHERE id = ?",
            params,
        )

    conn.execute(
        """
        INSERT OR IGNORE INTO artifact_hashes
            (artifact_id, algorithm, digest)
        VALUES (?, ?, ?)
        """,
        (artifact_id, algorithm, digest),
    )


def _insert_artifact(
    conn: sqlite3.Connection,
    *,
    columns: set[str],
    artifact_id: str,
    now: float,
    path: str,
    source_type: str | None,
    capture_method: str | None,
    hash_value: str | None,
    size: int = 0,
    metadata: str,
) -> None:
    try:
        normalized_size = max(0, int(size))
    except (TypeError, ValueError):
        normalized_size = 0

    insert_fields = ["id", "size", "first_seen_at", "first_seen_path", "kind", "metadata"]
    values: list[Any] = [artifact_id, normalized_size, now, path, "primitive", metadata]

    if "path" in columns:
        insert_fields.append("path")
        values.append(path)
    if "hash" in columns:
        insert_fields.append("hash")
        values.append(hash_value)
    if "source_type" in columns:
        insert_fields.append("source_type")
        values.append(source_type)
    if "capture_method" in columns:
        insert_fields.append("capture_method")
        values.append(capture_method)

    placeholders = ", ".join("?" for _ in insert_fields)
    field_list = ", ".join(insert_fields)
    conn.execute(
        f"INSERT OR IGNORE INTO artifacts ({field_list}) VALUES ({placeholders})",
        values,
    )


def _normalize_source_type(source_type: str | None, path: str) -> str | None:
    if source_type:
        lowered = source_type.strip().lower()
        return lowered or None
    if path.startswith("s3://"):
        return "s3"
    return None


def _normalize_capture_method(capture_method: str | None) -> str | None:
    if not capture_method:
        return None
    method = capture_method.strip().lower()
    return method or None


def _dependency_tokens_for_ref(ref: ArtifactRef) -> set[str]:
    tokens: set[str] = set()

    hash_value = _normalize_hash(_to_text(ref.hash))
    if hash_value:
        tokens.add(f"hash:{hash_value}")

    path = _to_text(ref.path)
    if path:
        tokens.add(f"path:{path}")

    return tokens


def _normalize_hash(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text or None


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:
            return value.decode("utf-8", errors="ignore")
    text = str(value)
    return text or None
