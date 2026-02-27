"""
roar Ray log collector.

Called from the driver process atexit handler (when ROAR_WRAP=1).
Collects worker events from a Ray actor aggregator when available, with
filesystem JSONL logs as a fallback. De-duplicates paths and inserts
artifact + job_input/output rows into ROAR_PROJECT_DIR/.roar/roar.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from roar.ray.fragment import ArtifactRef, TaskFragment
from roar.services.execution.proxy import parse_log_line

_READ_OPS = frozenset({"GetObject"})
_WRITE_OPS = frozenset({"PutObject", "CompleteMultipartUpload"})
_CAPTURE_PRIORITY = {"python": 0, "proxy": 1, "tracer": 2}


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


def collect(
    project_dir: str | None = None,
    log_dir: str | None = None,
    proxy_logs: dict[str, dict[str, Any]] | None = None,
) -> None:
    """
    Collect Ray worker I/O logs and write to the roar SQLite database.

    Args:
        project_dir: Directory containing the .roar/ subdirectory.
                     Defaults to ROAR_PROJECT_DIR env var, then "/app".
        log_dir:     Directory where worker JSONL logs were written.
                     Defaults to ROAR_LOG_DIR env var, then "/shared/.roar-logs".
    """
    if project_dir is None:
        project_dir = os.environ.get("ROAR_PROJECT_DIR", "/app")
    if log_dir is None:
        log_dir = os.environ.get("ROAR_LOG_DIR", "/shared/.roar-logs")

    db_path = os.path.join(project_dir, ".roar", "roar.db")
    log_path = Path(log_dir)

    if not os.path.exists(db_path):
        return  # roar not initialised; nothing to do

    actor_events: list[dict[str, Any]] | None = None
    actor_fragments: list[dict[str, Any]] = []
    actor_payload = _collect_actor_payload()
    if actor_payload is not None:
        actor_events, actor_fragments = actor_payload

    if actor_fragments and not actor_events:
        actor_events = _events_from_fragments(actor_fragments)

    if actor_fragments and not actor_events:
        collect_fragments(
            actor_fragments,
            project_dir=project_dir,
            driver_job_uid=os.environ.get("ROAR_DRIVER_JOB_UID"),
        )
        _consume_filesystem_logs(log_path)
        return

    task_events = _collect_events(log_path, actor_events=actor_events)
    _merge_proxy_logs(task_events, proxy_logs or {})
    if not task_events:
        return

    path_info = _aggregate_paths(task_events)
    if not path_info:
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        now = time.time()
        job_id = _create_ray_job(conn, now)
        artifact_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }

        for path, info in path_info.items():
            task_id = info["writer_task_id"] or info["task_id"]
            node_id = info["writer_node_id"] or info["node_id"]
            metadata_payload = {"ray_task_id": task_id}
            if node_id:
                metadata_payload["ray_node_id"] = node_id
            metadata = json.dumps(metadata_payload)

            _insert_artifact(
                conn,
                columns=artifact_columns,
                artifact_id=str(uuid.uuid4()),
                now=now,
                path=path,
                source_type=info["source_type"],
                capture_method=info["capture_method"],
                hash_value=info["hash"],
                size=info["size"],
                metadata=metadata,
            )

            row = conn.execute(
                "SELECT id FROM artifacts WHERE first_seen_path = ? ORDER BY first_seen_at DESC LIMIT 1",
                (path,),
            ).fetchone()
            if row is None:
                continue
            actual_artifact_id = row["id"]

            if info["saw_write"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO job_outputs
                        (job_id, artifact_id, path)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, actual_artifact_id, path),
                )
            if info["saw_read"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO job_inputs
                        (job_id, artifact_id, path)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, actual_artifact_id, path),
                )

        conn.commit()
        # Consume logs once to keep collection idempotent if collect() is called
        # multiple times during shutdown.
        _consume_filesystem_logs(log_path)
    finally:
        conn.close()


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


def _events_from_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for payload in fragments:
        if not isinstance(payload, dict):
            continue

        try:
            fragment = TaskFragment.from_dict(payload)
        except Exception:
            continue

        task_id = _to_text(fragment.ray_task_id) or _to_text(fragment.job_uid) or "unknown"
        node_id = _to_text(fragment.ray_node_id)

        for ref in fragment.reads:
            event: dict[str, Any] = {
                "path": ref.path,
                "mode": "r",
                "task_id": task_id,
                "capture_method": _normalize_capture_method(_to_text(ref.capture_method)),
                "size": _normalize_size(ref.size),
            }
            if node_id:
                event["node_id"] = node_id
            hash_value = _normalize_hash(_to_text(ref.hash))
            if hash_value:
                event["hash"] = hash_value
            events.append(event)

        for ref in fragment.writes:
            event = {
                "path": ref.path,
                "mode": "w",
                "task_id": task_id,
                "capture_method": _normalize_capture_method(_to_text(ref.capture_method)),
                "size": _normalize_size(ref.size),
            }
            if node_id:
                event["node_id"] = node_id
            hash_value = _normalize_hash(_to_text(ref.hash))
            if hash_value:
                event["hash"] = hash_value
            events.append(event)

    return events


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


def _collect_actor_payload() -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]] | None:
    try:
        import ray
    except Exception:
        return None

    is_initialized = getattr(ray, "is_initialized", None)
    if callable(is_initialized) and not is_initialized():
        return None

    job_id = os.environ.get("ROAR_JOB_ID", "default")
    actor_name = f"roar-log-collector-{job_id}"

    try:
        actor = ray.get_actor(actor_name, namespace="roar")
    except Exception:
        return None

    try:
        events: list[dict[str, Any]] | None = None
        get_all = getattr(actor, "get_all", None)
        get_all_remote = getattr(get_all, "remote", None) if get_all is not None else None
        if callable(get_all_remote):
            raw_events = ray.get(get_all_remote(), timeout=30)
            if isinstance(raw_events, list):
                events = [event for event in raw_events if isinstance(event, dict)]
            else:
                events = []

        fragments: list[dict[str, Any]] = []
        get_all_fragments = getattr(actor, "get_all_fragments", None)
        get_fragments_remote = (
            getattr(get_all_fragments, "remote", None) if get_all_fragments is not None else None
        )
        if callable(get_fragments_remote):
            raw_fragments = ray.get(get_fragments_remote(), timeout=30)
            if isinstance(raw_fragments, list):
                fragments = [fragment for fragment in raw_fragments if isinstance(fragment, dict)]

        return events, fragments
    except Exception:
        return None
    finally:
        with suppress(Exception):
            ray.kill(actor)


def _collect_events(
    log_path: Path,
    actor_events: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if actor_events is not None:
        return _group_events_by_task(actor_events)

    try:
        import ray

        if ray.is_initialized():
            actor_events = _collect_from_actor()
            if actor_events is not None:
                return _group_events_by_task(actor_events)
    except Exception:
        pass

    if not log_path.exists():
        return {}
    return _read_events(log_path)


def _consume_filesystem_logs(log_path: Path) -> None:
    if not log_path.exists():
        return
    for log_file in log_path.glob("*.jsonl"):
        with suppress(OSError):
            log_file.unlink()


def _collect_from_actor() -> list[dict[str, Any]] | None:
    try:
        import ray
    except Exception:
        return None

    job_id = os.environ.get("ROAR_JOB_ID", "default")
    actor_name = f"roar-log-collector-{job_id}"

    try:
        actor = ray.get_actor(actor_name, namespace="roar")
    except Exception:
        return None

    try:
        events = ray.get(actor.get_all.remote(), timeout=30)
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]
    except Exception:
        return None
    finally:
        with suppress(Exception):
            ray.kill(actor)


def _group_events_by_task(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    task_events: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        task_id = _to_text(event.get("task_id")) or "unknown"
        task_events.setdefault(task_id, []).append(event)
    return task_events


def _read_events(log_path: Path) -> dict[str, list[dict[str, Any]]]:
    # task_id -> list of event dicts
    task_events: dict[str, list[dict[str, Any]]] = {}
    logger = _get_logger()

    for log_file in sorted(log_path.glob("*.jsonl")):
        task_id = log_file.stem
        events: list[dict[str, Any]] = []
        try:
            for line_number, line in enumerate(
                log_file.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                    if isinstance(payload, dict):
                        events.append(payload)
                    else:
                        logger.warning(
                            "Skipping non-object JSON payload in Ray log %s line %d",
                            log_file,
                            line_number,
                        )
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed JSON line in Ray log %s line %d",
                        log_file,
                        line_number,
                    )
        except OSError as exc:
            logger.warning("Skipping unreadable Ray log %s: %s", log_file, exc)

        if events:
            task_events[task_id] = task_events.get(task_id, []) + events

    return task_events


def _merge_proxy_logs(
    task_events: dict[str, list[dict[str, Any]]],
    proxy_logs: dict[str, dict[str, Any]],
) -> None:
    for fallback_node_id, payload in proxy_logs.items():
        if not isinstance(payload, dict):
            continue

        node_id = _to_text(payload.get("node_id")) or _to_text(fallback_node_id)
        lines = payload.get("proxy_log_lines")
        if not isinstance(lines, list):
            continue

        task_id = f"proxy-{node_id or 'unknown'}"
        events = task_events.setdefault(task_id, [])
        for line in lines:
            if not isinstance(line, str):
                continue

            parsed = parse_log_line(line)
            if parsed is None:
                continue

            mode = "r" if parsed.operation in _READ_OPS else "w"
            event: dict[str, Any] = {
                "path": f"s3://{parsed.bucket}/{parsed.key}",
                "mode": mode,
                "task_id": task_id,
                "source_type": "s3",
                "capture_method": "proxy",
                "operation": parsed.operation,
            }
            if node_id:
                event["node_id"] = node_id
            if parsed.etag:
                event["hash"] = parsed.etag
            if parsed.size_bytes is not None:
                event["size"] = _normalize_size(parsed.size_bytes)

            events.append(event)


def _aggregate_paths(task_events: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    # path -> rollup info
    path_info: dict[str, dict[str, Any]] = {}

    for fallback_task_id, events in task_events.items():
        for event in events:
            raw_path = event.get("path")
            path = _to_text(raw_path)
            if not path:
                continue

            event_task_id = _to_text(event.get("task_id")) or fallback_task_id
            event_node_id = _to_text(event.get("node_id"))
            operation = _to_text(event.get("operation"))
            mode = _to_text(event.get("mode")) or "r"

            is_read, is_write = _infer_direction(mode, operation)
            source_type = _normalize_source_type(_to_text(event.get("source_type")), path)
            capture_method = _normalize_capture_method(_to_text(event.get("capture_method")))
            hash_value = _normalize_hash(_to_text(event.get("hash")))
            event_size = _normalize_size(event.get("size"))

            if path not in path_info:
                path_info[path] = {
                    "task_id": event_task_id,
                    "node_id": event_node_id,
                    "writer_task_id": None,
                    "writer_node_id": None,
                    "saw_read": False,
                    "saw_write": False,
                    "source_type": source_type,
                    "capture_method": capture_method,
                    "hash": hash_value,
                    "size": event_size,
                }

            info = path_info[path]

            if event_node_id and not info["node_id"]:
                info["node_id"] = event_node_id

            if source_type and not info["source_type"]:
                info["source_type"] = source_type

            info["capture_method"] = _choose_capture_method(info["capture_method"], capture_method)

            if hash_value and not info["hash"]:
                info["hash"] = hash_value
            if event_size > info["size"]:
                info["size"] = event_size

            if is_write:
                info["saw_write"] = True
                if info["writer_task_id"] is None:
                    info["writer_task_id"] = event_task_id
                    info["writer_node_id"] = event_node_id

            if is_read:
                info["saw_read"] = True

    return path_info


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


def _infer_direction(mode: str, operation: str | None) -> tuple[bool, bool]:
    if operation in _WRITE_OPS:
        return False, True
    if operation in _READ_OPS:
        return True, False

    is_write = any(flag in mode for flag in ("w", "a", "x", "+"))
    is_read = "r" in mode or "+" in mode or not is_write
    return is_read, is_write


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


def _choose_capture_method(existing: str | None, incoming: str | None) -> str | None:
    if not incoming:
        return existing
    if not existing:
        return incoming
    existing_rank = _CAPTURE_PRIORITY.get(existing, -1)
    incoming_rank = _CAPTURE_PRIORITY.get(incoming, -1)
    return incoming if incoming_rank >= existing_rank else existing


def _normalize_hash(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text or None


def _normalize_size(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
