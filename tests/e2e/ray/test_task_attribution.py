"""
TDD: roar attributes file I/O to specific Ray tasks.

Per-task attribution is the highest-value lineage feature — knowing not just
*what* was read/written, but *which task* did it.

These tests FAIL until roar injects task context into workers and records
task_id alongside each I/O event.

Run against a live cluster:
    pytest tests/e2e/ray/test_task_attribution.py -v --timeout=120
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.e2e.ray.conftest import submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


class TestTaskAttribution:
    """Each I/O event is tagged with the Ray task ID that caused it."""

    def test_each_output_has_task_id(self, ray_cluster):
        """
        Every artifact written by a Ray task should be tagged with the
        Ray task ID (from ray.get_runtime_context().get_task_id()).

        FAILS until roar captures task context alongside file I/O.
        """
        _stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/attributed_file_io.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        # roar should record task_id in artifact metadata or a separate table
        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT a.first_seen_path AS path, a.metadata "
            "FROM artifacts a "
            "WHERE a.first_seen_path LIKE '%attributed%'",
        )
        assert len(rows) >= 6, (
            f"Expected 6 attributed output files, got {len(rows)}. Workers may not be instrumented."
        )

        missing_task_id = []
        for row in rows:
            metadata = json.loads(row["metadata"] or "{}")
            if not metadata.get("ray_task_id"):
                missing_task_id.append(row["path"])

        assert not missing_task_id, (
            f"These artifacts are missing ray_task_id in metadata: {missing_task_id}. "
            "roar is not yet recording Ray task context with I/O events."
        )

    def test_distinct_tasks_produce_distinct_attributions(self, ray_cluster):
        """
        Six tasks writing six different files should produce six distinct
        task IDs in the lineage records.

        FAILS until per-task attribution is implemented.
        """
        submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/attributed_file_io.py",
            env={"ROAR_WRAP": "1"},
        )

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT DISTINCT json_extract(metadata, '$.ray_task_id') as task_id "
            "FROM artifacts "
            "WHERE first_seen_path LIKE '%attributed%' AND metadata IS NOT NULL",
        )
        task_ids = {r["task_id"] for r in rows if r["task_id"]}
        assert len(task_ids) >= 6, (
            f"Expected ≥ 6 distinct ray_task_ids, got {len(task_ids)}: {task_ids}. "
            "Each task must be independently attributed."
        )

    def test_reader_task_linked_to_writer_tasks(self, ray_cluster):
        """
        The task that reads the outputs of multiple writer tasks should have
        those files recorded as its inputs, creating a task-level DAG edge.

        FAILS until roar tracks per-task input/output relationships.
        """
        submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/attributed_file_io.py",
            env={"ROAR_WRAP": "1"},
        )

        # The reader task reads 6 files written by 6 writer tasks
        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT COUNT(*) as cnt "
            "FROM job_inputs ji "
            "JOIN artifacts a ON ji.artifact_id = a.id "
            "WHERE ji.path LIKE '%attributed%'",
        )
        count = rows[0]["cnt"] if rows else 0
        assert count >= 6, (
            f"Expected ≥ 6 reader-task inputs, got {count}. "
            "Reader task is not recording its file reads as lineage inputs."
        )
