"""
roar Ray log collector.

Called from the driver process atexit handler (when ROAR_WRAP=1).
Scans ROAR_LOG_DIR for per-task JSONL logs written by workers,
de-duplicates paths, and inserts artifact + job_input/output rows
into the roar DB at ROAR_PROJECT_DIR/.roar/roar.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def collect(
    project_dir: str | None = None,
    log_dir: str | None = None,
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
    if not log_path.exists():
        return  # no worker logs produced

    # -------------------------------------------------------------------------
    # Parse all JSONL log files.
    # Each file is named <task_id>.jsonl and contains one JSON object per line:
    #   {"path": "/abs/path", "mode": "r", "task_id": "...", "ts": 1234.5}
    # -------------------------------------------------------------------------
    # task_id -> list of {"path", "mode"} dicts
    task_events: dict[str, list[dict]] = {}

    for log_file in sorted(log_path.glob("*.jsonl")):
        task_id = log_file.stem
        events: list[dict] = []
        try:
            for line in log_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass
        if events:
            task_events[task_id] = task_events.get(task_id, []) + events

    if not task_events:
        return

    # -------------------------------------------------------------------------
    # Aggregate by path while preserving both input and output edges.
    # If a path is written by one task and read by another, we:
    #   - keep the writer task id on artifact metadata
    #   - emit both job_outputs and job_inputs rows for the same artifact/path
    # -------------------------------------------------------------------------
    # path -> {"task_id": str, "writer_task_id": str | None, "saw_read": bool, "saw_write": bool}
    path_info: dict[str, dict] = {}

    for task_id, events in task_events.items():
        for ev in events:
            path = ev.get("path", "")
            mode = str(ev.get("mode", "r"))
            if not path:
                continue

            is_write = any(flag in mode for flag in ("w", "a", "x", "+"))
            is_read = "r" in mode or "+" in mode or not is_write

            if path not in path_info:
                path_info[path] = {
                    "task_id": task_id,  # fallback when no writer exists
                    "writer_task_id": None,
                    "saw_read": False,
                    "saw_write": False,
                }

            if is_write:
                path_info[path]["saw_write"] = True
                if path_info[path]["writer_task_id"] is None:
                    path_info[path]["writer_task_id"] = task_id

            if is_read:
                path_info[path]["saw_read"] = True

    if not path_info:
        return

    # -------------------------------------------------------------------------
    # Write to the roar DB.
    # -------------------------------------------------------------------------
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        now = time.time()
        job_uid = str(uuid.uuid4())

        # Create a job record for this Ray run.
        conn.execute(
            """
            INSERT INTO jobs
                (job_uid, command, script, timestamp, status, job_type)
            VALUES
                (?, ?, ?, ?, ?, ?)
            """,
            (job_uid, "ray", None, now, "completed", "ray"),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for path, info in path_info.items():
            task_id = info["writer_task_id"] or info["task_id"]
            artifact_id = str(uuid.uuid4())
            metadata = json.dumps({"ray_task_id": task_id})

            # Upsert artifact (ignore duplicates on first_seen_path).
            conn.execute(
                """
                INSERT OR IGNORE INTO artifacts
                    (id, size, first_seen_at, first_seen_path, kind, metadata)
                VALUES
                    (?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, 0, now, path, "primitive", metadata),
            )

            # Retrieve the actual artifact_id (may already exist).
            row = conn.execute(
                "SELECT id FROM artifacts WHERE first_seen_path = ?", (path,)
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
    finally:
        conn.close()
