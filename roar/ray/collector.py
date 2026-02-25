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
from pathlib import Path
from typing import Any

from roar.services.execution.proxy import parse_log_line

_READ_OPS = frozenset({"GetObject"})
_WRITE_OPS = frozenset({"PutObject", "CompleteMultipartUpload"})
_CAPTURE_PRIORITY = {"python": 0, "proxy": 1, "tracer": 2}


def _get_logger():
    from roar.core.logging import get_logger  # noqa: PLC0415

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

    task_events = _collect_events(log_path)
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
        if log_path.exists():
            for log_file in log_path.glob("*.jsonl"):
                try:
                    log_file.unlink()
                except OSError:
                    pass
    finally:
        conn.close()


def _collect_events(log_path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        import ray  # noqa: PLC0415

        if ray.is_initialized():
            actor_events = _collect_from_actor()
            if actor_events is not None:
                return _group_events_by_task(actor_events)
    except Exception:  # noqa: BLE001
        pass

    if not log_path.exists():
        return {}
    return _read_events(log_path)


def _collect_from_actor() -> list[dict[str, Any]] | None:
    try:
        import ray  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    job_id = os.environ.get("ROAR_JOB_ID", "default")
    actor_name = f"roar-log-collector-{job_id}"

    try:
        actor = ray.get_actor(actor_name, namespace="roar")
    except Exception:  # noqa: BLE001
        return None

    try:
        events = ray.get(actor.get_all.remote(), timeout=30)
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            ray.kill(actor)
        except Exception:  # noqa: BLE001
            pass


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
                }

            info = path_info[path]

            if event_node_id and not info["node_id"]:
                info["node_id"] = event_node_id

            if source_type and not info["source_type"]:
                info["source_type"] = source_type

            info["capture_method"] = _choose_capture_method(
                info["capture_method"], capture_method
            )

            if hash_value and not info["hash"]:
                info["hash"] = hash_value

            if is_write:
                info["saw_write"] = True
                if info["writer_task_id"] is None:
                    info["writer_task_id"] = event_task_id
                    info["writer_node_id"] = event_node_id

            if is_read:
                info["saw_read"] = True

    return path_info


def _create_ray_job(conn: sqlite3.Connection, now: float) -> int:
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
    metadata: str,
) -> None:
    insert_fields = ["id", "size", "first_seen_at", "first_seen_path", "kind", "metadata"]
    values: list[Any] = [artifact_id, 0, now, path, "primitive", metadata]

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


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:  # noqa: BLE001
            return value.decode("utf-8", errors="ignore")
    text = str(value)
    return text or None
