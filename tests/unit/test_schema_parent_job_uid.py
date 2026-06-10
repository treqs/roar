from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.db.context import create_database_context
from roar.db.schema import run_migrations


def _create_legacy_db(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uid TEXT UNIQUE,
            timestamp REAL NOT NULL,
            command TEXT NOT NULL,
            script TEXT,
            step_identity TEXT,
            session_id INTEGER,
            step_number INTEGER,
            step_name TEXT,
            git_repo TEXT,
            git_commit TEXT,
            git_branch TEXT,
            duration_seconds REAL,
            exit_code INTEGER,
            synced_at REAL,
            status TEXT,
            job_type TEXT,
            metadata TEXT,
            telemetry TEXT
        );
        CREATE TABLE IF NOT EXISTS job_inputs (
            job_id INTEGER NOT NULL,
            artifact_id TEXT NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (job_id, artifact_id, path)
        );
        CREATE TABLE IF NOT EXISTS job_outputs (
            job_id INTEGER NOT NULL,
            artifact_id TEXT NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (job_id, artifact_id, path)
        );
        """
    )
    return conn


def test_run_migrations_adds_parent_job_uid_column() -> None:
    conn = _create_legacy_db()

    run_migrations(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "parent_job_uid" in columns
    assert "execution_backend" in columns
    assert "execution_role" in columns


def test_insert_job_with_parent_job_uid_succeeds() -> None:
    conn = _create_legacy_db()
    run_migrations(conn)

    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, parent_job_uid)
        VALUES (?, ?, ?, ?)
        """,
        ("child-uid", 1.0, "ray_task:train", "abc"),
    )
    conn.commit()

    row = conn.execute(
        "SELECT parent_job_uid FROM jobs WHERE job_uid = ?",
        ("child-uid",),
    ).fetchone()
    assert row is not None
    assert row["parent_job_uid"] == "abc"


def test_insert_job_with_null_parent_job_uid_succeeds() -> None:
    conn = _create_legacy_db()
    run_migrations(conn)

    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, parent_job_uid)
        VALUES (?, ?, ?, ?)
        """,
        ("driver-uid", 2.0, "python train.py", None),
    )
    conn.commit()

    row = conn.execute(
        "SELECT parent_job_uid FROM jobs WHERE job_uid = ?",
        ("driver-uid",),
    ).fetchone()
    assert row is not None
    assert row["parent_job_uid"] is None


def test_create_database_context_migrates_parent_job_uid_for_legacy_db(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    db_path = roar_dir / "roar.db"

    conn = _create_legacy_db(str(db_path))
    conn.commit()
    conn.close()

    with create_database_context(roar_dir):
        pass

    migrated = sqlite3.connect(str(db_path))
    migrated.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()}
    finally:
        migrated.close()

    assert "parent_job_uid" in columns
    assert "execution_backend" in columns
    assert "execution_role" in columns
