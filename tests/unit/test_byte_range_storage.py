"""Tests for byte_ranges storage, accumulation, and retrieval in the job repository."""

import json
import time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from roar.db.models import Base
from roar.db.repositories.job import SQLAlchemyJobRepository
from roar.db.schema import run_migrations


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database with all tables."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def artifact_repo():
    """Mock artifact repository."""
    mock = MagicMock()
    mock.get_hashes.return_value = [{"algorithm": "blake3", "digest": "abc123"}]
    return mock


@pytest.fixture
def job_repo(db_session, artifact_repo):
    """Create a job repository with the test session."""
    return SQLAlchemyJobRepository(db_session, artifact_repo)


def _create_job(db_session) -> int:
    """Insert a minimal job row and return its id."""
    db_session.execute(
        text("INSERT INTO jobs (job_uid, timestamp, command) VALUES (:uid, :ts, :cmd)"),
        {"uid": "test1234", "ts": time.time(), "cmd": "python train.py"},
    )
    db_session.flush()
    result = db_session.execute(text("SELECT id FROM jobs WHERE job_uid = 'test1234'"))
    return result.scalar_one()


def _create_artifact(db_session, artifact_id: str = "art-001") -> str:
    """Insert a minimal artifact row and return its id."""
    db_session.execute(
        text(
            "INSERT INTO artifacts (id, size, first_seen_at, first_seen_path) "
            "VALUES (:id, :size, :ts, :path)"
        ),
        {"id": artifact_id, "size": 10000, "ts": time.time(), "path": "/data/file.csv"},
    )
    db_session.flush()
    return artifact_id


class TestAddInputWithByteRanges:
    def test_add_input_with_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_input(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[0, 999]])

        row = db_session.execute(
            text("SELECT byte_ranges FROM job_inputs WHERE job_id = :jid"),
            {"jid": job_id},
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == [[0, 999]]

    def test_add_input_without_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_input(job_id, artifact_id, "s3://bucket/key")

        row = db_session.execute(
            text("SELECT byte_ranges FROM job_inputs WHERE job_id = :jid"),
            {"jid": job_id},
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_add_input_accumulates_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_input(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[0, 999]])
        job_repo.add_input(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[2000, 2999]])

        row = db_session.execute(
            text("SELECT byte_ranges FROM job_inputs WHERE job_id = :jid"),
            {"jid": job_id},
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == [[0, 999], [2000, 2999]]

    def test_get_inputs_returns_parsed_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_input(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[0, 999]])

        inputs = job_repo.get_inputs(job_id)
        assert len(inputs) == 1
        assert inputs[0]["byte_ranges"] == [[0, 999]]

    def test_get_inputs_returns_none_when_no_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_input(job_id, artifact_id, "s3://bucket/key")

        inputs = job_repo.get_inputs(job_id)
        assert len(inputs) == 1
        assert inputs[0]["byte_ranges"] is None


class TestAddOutputWithByteRanges:
    def test_add_output_with_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_output(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[0, 4095]])

        row = db_session.execute(
            text("SELECT byte_ranges FROM job_outputs WHERE job_id = :jid"),
            {"jid": job_id},
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == [[0, 4095]]

    def test_add_output_accumulates_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_output(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[0, 999]])
        job_repo.add_output(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[5000, 5999]])

        row = db_session.execute(
            text("SELECT byte_ranges FROM job_outputs WHERE job_id = :jid"),
            {"jid": job_id},
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == [[0, 999], [5000, 5999]]

    def test_get_outputs_returns_parsed_byte_ranges(self, job_repo, db_session):
        job_id = _create_job(db_session)
        artifact_id = _create_artifact(db_session)

        job_repo.add_output(job_id, artifact_id, "s3://bucket/key", byte_ranges=[[0, 4095]])

        outputs = job_repo.get_outputs(job_id)
        assert len(outputs) == 1
        assert outputs[0]["byte_ranges"] == [[0, 4095]]


class TestSchemaMigration:
    def test_schema_migration_adds_byte_ranges_columns(self, tmp_path):
        """Verify migration adds byte_ranges to existing tables without the column."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Create old schema without byte_ranges
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
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
        """)

        # Verify byte_ranges column does not exist
        cursor = conn.execute("PRAGMA table_info(job_inputs)")
        input_columns = {row["name"] for row in cursor.fetchall()}
        assert "byte_ranges" not in input_columns

        # Run migration
        run_migrations(conn)

        # Verify byte_ranges column was added to both tables
        cursor = conn.execute("PRAGMA table_info(job_inputs)")
        input_columns = {row["name"] for row in cursor.fetchall()}
        assert "byte_ranges" in input_columns

        cursor = conn.execute("PRAGMA table_info(job_outputs)")
        output_columns = {row["name"] for row in cursor.fetchall()}
        assert "byte_ranges" in output_columns

        conn.close()
