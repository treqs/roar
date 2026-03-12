from __future__ import annotations

import sqlite3

import pytest

from roar.backends.ray import collector
from roar.db.schema import SCHEMA


def test_create_ray_job_reuses_existing_job_from_roar_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, status, job_type)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("job-existing-123", 1.0, "python train.py", "running", "run"),
    )
    existing_id = int(conn.execute("SELECT id FROM jobs").fetchone()["id"])
    monkeypatch.setenv("ROAR_JOB_ID", "job-existing-123")

    resolved_id = collector._create_ray_job(conn, now=2.0)

    assert resolved_id == existing_id
    assert int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) == 1
    status = conn.execute("SELECT status FROM jobs WHERE id = ?", (existing_id,)).fetchone()[
        "status"
    ]
    assert status == "completed"
