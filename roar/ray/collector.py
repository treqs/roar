"""
roar Ray log collector.

Called from the driver process atexit handler (when ROAR_WRAP=1).
In Phase 2, Ray lineage is fragments-only:
FragmentReconstituter fetches encrypted fragment batches from GLaaS and this
module merges those fragments into ROAR_PROJECT_DIR/.roar/roar.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import deque
from typing import Any

from roar.ray.fragment import ArtifactRef, TaskFragment


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


def collect(
    project_dir: str | None = None,
    log_dir: str | None = None,
    proxy_logs: dict[str, dict[str, Any]] | None = None,
    fragments: list[dict] | None = None,
) -> None:
    """
    Compatibility entrypoint that now only accepts explicit fragments.
    """
    del log_dir, proxy_logs

    if not fragments:
        return

    if project_dir is None:
        project_dir = os.environ.get("ROAR_PROJECT_DIR", "/app")

    db_path = os.path.join(project_dir, ".roar", "roar.db")
    if not os.path.exists(db_path):
        return  # roar not initialised; nothing to do

    session_id, base_step = _resolve_active_session_context(db_path)
    collect_fragments(
        fragments=fragments,
        project_dir=project_dir,
        driver_job_uid=os.environ.get("ROAR_JOB_ID"),
        session_id=session_id,
        step_number=base_step,
    )


def collect_fragments(
    fragments: list[dict],
    project_dir: str | None = None,
    driver_job_uid: str | None = None,
    session_id: int | None = None,
    step_number: int = 1,
) -> None:
    """Write Ray task fragments to the local DB as child jobs."""
    if project_dir is None:
        project_dir = os.environ.get("ROAR_PROJECT_DIR", "/app")

    db_path = os.path.join(project_dir, ".roar", "roar.db")
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        artifact_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        now = time.time()
        parsed_fragments: list[TaskFragment] = []
        for payload in fragments:
            if not isinstance(payload, dict):
                continue

            try:
                fragment = TaskFragment.from_dict(payload)
            except Exception:
                continue
            parsed_fragments.append(fragment)

        step_by_job_uid = _assign_step_numbers(parsed_fragments, base_step=step_number)

        for fragment in parsed_fragments:
            fragment_step_number = step_by_job_uid.get(fragment.job_uid, step_number + 1)

            _insert_fragment_job(
                conn=conn,
                job_columns=job_columns,
                fragment=fragment,
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

            for ref in fragment.reads:
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

            for ref in fragment.writes:
                artifact_id = _upsert_artifact_for_ref(
                    conn,
                    columns=artifact_columns,
                    ref=ref,
                    now=now,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO job_outputs
                        (job_id, artifact_id, path)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, artifact_id, ref.path),
                )

        conn.commit()
    finally:
        conn.close()


def _assign_step_numbers(
    fragments: list[TaskFragment],
    base_step: int = 1,
) -> dict[str, int]:
    """
    Return {job_uid: step_number} using artifact dependency topology.

    Fragments are assigned step numbers based on their depth in the DAG
    formed by artifact read/write dependencies. A fragment that reads an
    artifact written by another fragment is at a strictly higher step.

    base_step: the step number of the parent driver job (default 1).
    Returns step numbers starting at base_step + 1.
    """
    if not fragments:
        return {}

    # Fragments are emitted as incremental snapshots per task/job_uid.
    # Collapse snapshots first so they do not create synthetic self-chains.
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
            hash_value = _normalize_hash(_to_text(ref.hash))
            if hash_value:
                reads_by_job[job_index].add(hash_value)

        for ref in fragment.writes:
            hash_value = _normalize_hash(_to_text(ref.hash))
            if hash_value:
                writes_by_job[job_index].add(hash_value)

    # hash -> producer job indices
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


def _insert_fragment_job(
    conn: sqlite3.Connection,
    *,
    job_columns: set[str],
    fragment: TaskFragment,
    driver_job_uid: str | None,
    session_id: int | None,
    step_number: int,
    now: float,
) -> None:
    command = f"ray_task:{fragment.function_name}" if fragment.function_name else "ray_task"
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
        fragment.function_name,
        duration,
        fragment.exit_code,
        "completed",
        "ray_task",
    ]

    if "parent_job_uid" in job_columns:
        fields.append("parent_job_uid")
        values.append(parent_job_uid)
    if "session_id" in job_columns and session_id is not None:
        fields.append("session_id")
        values.append(session_id)
    if "step_number" in job_columns:
        fields.append("step_number")
        values.append(step_number)
    if "metadata" in job_columns:
        metadata = {
            "ray_task_id": fragment.ray_task_id,
            "ray_worker_id": fragment.ray_worker_id,
            "ray_node_id": fragment.ray_node_id,
        }
        if fragment.ray_actor_id:
            metadata["ray_actor_id"] = fragment.ray_actor_id
        fields.append("metadata")
        values.append(json.dumps(metadata))

    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT OR IGNORE INTO jobs ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )


def _upsert_artifact_for_ref(
    conn: sqlite3.Connection,
    *,
    columns: set[str],
    ref: ArtifactRef,
    now: float,
) -> str:
    digest = _normalize_hash(_to_text(ref.hash))
    algorithm = _to_text(ref.hash_algorithm)

    if digest and algorithm:
        row = conn.execute(
            """
            SELECT artifact_id
            FROM artifact_hashes
            WHERE algorithm = ? AND digest = ?
            LIMIT 1
            """,
            (algorithm, digest),
        ).fetchone()
        if row is not None:
            return str(row["artifact_id"])

    if not digest:
        existing = conn.execute(
            "SELECT id FROM artifacts WHERE first_seen_path = ? ORDER BY first_seen_at DESC LIMIT 1",
            (ref.path,),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])

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


def _resolve_active_session_context(db_path: str) -> tuple[int | None, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, current_step
            FROM sessions
            WHERE is_active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None, 1
        current_step = int(row["current_step"] or 1)
        return int(row["id"]), max(1, current_step)
    finally:
        conn.close()


def _create_ray_job(conn: sqlite3.Connection, now: float) -> int:
    roar_job_id = os.environ.get("ROAR_JOB_ID")
    if roar_job_id:
        existing_by_uid = conn.execute(
            "SELECT id FROM jobs WHERE job_uid = ? ORDER BY id DESC LIMIT 1",
            (roar_job_id,),
        ).fetchone()
        if existing_by_uid is not None:
            job_id = int(existing_by_uid["id"])
            conn.execute(
                """
                UPDATE jobs
                SET timestamp = ?, status = ?
                WHERE id = ?
                """,
                (now, "completed", job_id),
            )
            return job_id

    existing = conn.execute(
        "SELECT id FROM jobs WHERE job_type = 'ray' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if existing is not None:
        job_id = int(existing["id"])
        conn.execute(
            """
            UPDATE jobs
            SET timestamp = ?, command = ?, status = ?
            WHERE id = ?
            """,
            (now, "ray", "completed", job_id),
        )
        return job_id

    job_uid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO jobs
            (job_uid, command, script, timestamp, status, job_type)
        VALUES
            (?, ?, ?, ?, ?, ?)
        """,
        (job_uid, "ray", None, now, "completed", "ray"),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


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
