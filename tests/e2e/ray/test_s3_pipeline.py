"""E2E coverage for a 3-stage S3 Ray pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import run_docker, submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


def _parse_run_id(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    raise AssertionError(f"Unable to parse run_id from output:\n{stdout}")


def _run_pipeline() -> str:
    stdout, stderr, returncode = submit_job_on_head(
        COMPOSE_FILE,
        f"{JOBS_DIR}/s3_pipeline.py",
        env={"ROAR_WRAP": "1"},
    )
    assert returncode == 0, f"Job failed:\n{stderr}\n{stdout}"
    return _parse_run_id(stdout)


@pytest.fixture(autouse=True)
def reset_roar_state(ray_cluster):
    del ray_cluster
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
            "rm -rf /app/.roar /shared/.roar-logs && roar init --path /app -n",
        ],
        check=False,
        capture_output=True,
    )
    yield


class TestS3Pipeline:
    def test_all_s3_put_get_captured(self, ray_cluster):
        del ray_cluster
        run_id = _run_pipeline()

        rows = _query_roar_db(
            COMPOSE_FILE,
            """
            SELECT path
            FROM artifacts
            WHERE source_type IN ('s3', 'proxy')
              AND path LIKE ?
            """,
            (f"%/{run_id}/%",),
        )
        assert len(rows) >= 13, f"Expected >=13 distinct S3 artifacts, got {len(rows)}: {rows}"

    def test_cross_task_s3_artifact_identity(self, ray_cluster):
        del ray_cluster
        run_id = _run_pipeline()

        rows = _query_roar_db(
            COMPOSE_FILE,
            """
            SELECT a.id,
                   a.path,
                   COUNT(DISTINCT jo.job_id) AS producer_count,
                   COUNT(DISTINCT ji.job_id) AS consumer_count
            FROM artifacts a
            LEFT JOIN job_outputs jo ON jo.artifact_id = a.id
            LEFT JOIN job_inputs ji ON ji.artifact_id = a.id
            WHERE a.path LIKE ?
            GROUP BY a.id
            HAVING producer_count > 0 AND consumer_count > 0
            """,
            (f"%processed/{run_id}/%",),
        )
        assert len(rows) == 3, (
            f"Expected 3 processed shard artifacts with both producer and consumer, got {len(rows)}."
        )
        for row in rows:
            assert row["producer_count"] == 1, f"{row['path']}: expected 1 producer"
            assert row["consumer_count"] == 1, f"{row['path']}: expected 1 consumer"

    def test_model_artifacts_have_cross_task_identity(self, ray_cluster):
        del ray_cluster
        run_id = _run_pipeline()

        rows = _query_roar_db(
            COMPOSE_FILE,
            """
            SELECT a.id,
                   a.path,
                   COUNT(DISTINCT jo.job_id) AS producer_count,
                   COUNT(DISTINCT ji.job_id) AS consumer_count
            FROM artifacts a
            LEFT JOIN job_outputs jo ON jo.artifact_id = a.id
            LEFT JOIN job_inputs ji ON ji.artifact_id = a.id
            WHERE a.path LIKE ?
            GROUP BY a.id
            HAVING producer_count > 0 AND consumer_count > 0
            """,
            (f"%models/{run_id}/%",),
        )
        assert len(rows) == 3, (
            f"Expected 3 model artifacts with both producer and consumer, got {len(rows)}."
        )
        for row in rows:
            assert row["producer_count"] == 1, f"{row['path']}: expected 1 producer"
            assert row["consumer_count"] == 1, f"{row['path']}: expected 1 consumer"

    def test_no_orphaned_s3_artifacts(self, ray_cluster):
        del ray_cluster
        run_id = _run_pipeline()

        rows = _query_roar_db(
            COMPOSE_FILE,
            """
            SELECT a.path,
                   COUNT(DISTINCT jo.job_id) AS producers,
                   COUNT(DISTINCT ji.job_id) AS consumers
            FROM artifacts a
            LEFT JOIN job_outputs jo ON jo.artifact_id = a.id
            LEFT JOIN job_inputs ji ON ji.artifact_id = a.id
            WHERE a.path LIKE ?
               OR a.path LIKE ?
               OR a.path LIKE ?
            GROUP BY a.id
            """,
            (
                f"%processed/{run_id}/%",
                f"%models/{run_id}/%",
                f"%metrics/{run_id}/%",
            ),
        )
        assert len(rows) > 0
        for row in rows:
            assert row["producers"] >= 1, f"Orphaned artifact (no producer): {row['path']}"
            assert row["consumers"] >= 1, f"Dangling artifact (no consumer): {row['path']}"

    def test_lineage_depth_reaches_raw_inputs(self, ray_cluster):
        del ray_cluster
        run_id = _run_pipeline()

        report_jobs = _query_roar_db(
            COMPOSE_FILE,
            """
            SELECT j.id, j.command
            FROM jobs j
            JOIN job_outputs jo ON jo.job_id = j.id
            JOIN artifacts a ON jo.artifact_id = a.id
            WHERE a.path LIKE ?
            """,
            (f"%results/{run_id}/final_report.json",),
        )
        assert len(report_jobs) >= 1, "No job found that wrote final_report"

        visited_jobs: set[int] = set()
        frontier = {int(report_jobs[0]["id"])}
        depth = 0
        all_artifact_paths: set[str] = set()

        while frontier and depth < 8:
            next_frontier: set[int] = set()
            for job_id in frontier:
                if job_id in visited_jobs:
                    continue
                visited_jobs.add(job_id)
                inputs = _query_roar_db(
                    COMPOSE_FILE,
                    """
                    SELECT a.id,
                           a.path,
                           (
                               SELECT jo2.job_id
                               FROM job_outputs jo2
                               WHERE jo2.artifact_id = a.id
                               LIMIT 1
                           ) AS producer_job_id
                    FROM job_inputs ji
                    JOIN artifacts a ON ji.artifact_id = a.id
                    WHERE ji.job_id = ?
                    """,
                    (job_id,),
                )
                for inp in inputs:
                    path = inp.get("path")
                    if isinstance(path, str):
                        all_artifact_paths.add(path)
                    producer_job_id = inp.get("producer_job_id")
                    if producer_job_id is not None:
                        next_frontier.add(int(producer_job_id))
            frontier = next_frontier
            depth += 1

        raw_paths = [path for path in all_artifact_paths if f"raw/{run_id}/" in path]
        assert len(raw_paths) == 3, (
            f"Expected 3 raw shard paths in lineage, got {len(raw_paths)}. "
            f"All ancestor paths: {sorted(all_artifact_paths)}"
        )
