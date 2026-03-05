"""
TDD: roar captures file I/O from Ray workers.

These tests define the target behaviour for the roar-Ray integration.
They FAIL until roar's sitecustomize / tracer injection reaches workers.

Run against a live cluster:
    pytest tests/e2e/ray/test_file_io_capture.py -v --timeout=120
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import run_docker, submit_job_on_head

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


def _query_roar_db(compose_file, sql: str, params: tuple = ()) -> list[dict]:
    """
    Run a query against .roar/roar.db inside the ray-head container
    by exporting it and reading locally.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    run_docker(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "cp",
            "ray-head:/app/.roar/roar.db",
            tmp_path,
        ],
        check=True,
        capture_output=True,
    )

    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
        Path(tmp_path).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def reset_roar_state(ray_cluster):
    """Reset roar state on the head node before each test."""
    run_docker(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "ray-head",
            "bash",
            "-c",
            # Reset the roar DB and clear stale worker logs so previous
            # tests don't pollute the next one.
            "rm -rf /app/.roar /shared/.roar-logs && roar init --path /app -n",
        ],
        check=False,
        capture_output=True,
    )
    yield


class TestFileIOCapture:
    """roar captures file writes from @ray.remote tasks."""

    def test_worker_file_write_appears_as_output_artifact(self, ray_cluster):
        """
        roar run wrapping a Ray job should record files written by workers
        as output artifacts in the lineage DB.

        FAILS until roar instruments Ray workers.
        """
        _stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/basic_file_io.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        # The job writes /shared/output.json from a remote task.
        # roar should have captured this as an output artifact.
        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT first_seen_path FROM artifacts WHERE first_seen_path LIKE '%output.json'",
        )
        assert len(rows) >= 1, (
            "Expected /shared/output.json to appear in roar artifacts, "
            "but it was not captured. "
            "roar is not yet instrumenting Ray worker processes."
        )

    def test_worker_file_read_appears_as_input_artifact(self, ray_cluster):
        """
        Files read by Ray workers should appear as input artifacts.

        FAILS until roar instruments Ray workers.
        """
        _stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/basic_file_io.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT ji.path FROM job_inputs ji JOIN artifacts a ON ji.artifact_id = a.id "
            "WHERE ji.path LIKE '%input.json'",
        )
        assert len(rows) >= 1, (
            "Expected /shared/input.json to appear as a job input, "
            "but it was not captured from the Ray worker."
        )

    def test_pipeline_intermediate_files_captured(self, ray_cluster):
        """
        Multi-step pipeline: intermediate files produced and consumed
        by different tasks should all appear in lineage.

        FAILS until roar instruments Ray workers.
        """
        _stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/pipeline.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT first_seen_path FROM artifacts WHERE first_seen_path LIKE '/shared/%'",
        )
        captured_paths = {r["first_seen_path"] for r in rows}

        assert any("pipeline_input.csv" in p for p in captured_paths), (
            "pipeline_input.csv not captured"
        )
        assert any(".parquet" in p for p in captured_paths), "parquet output not captured"
