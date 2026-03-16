"""E2E coverage for a 3-stage S3 Ray pipeline through the host submit path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.backends.ray.e2e.conftest import (
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


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


def _run_pipeline(project_dir: Path, ray_cluster: dict[str, str]) -> str:
    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "s3_pipeline.py",
        use_fragment_store=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return _parse_run_id(result.stdout)


class TestS3Pipeline:
    @pytest.fixture(autouse=True)
    def _init_project(self) -> None:
        self.project_dir = make_host_project_dir("s3-pipeline")
        init_host_project(self.project_dir)

    def test_all_s3_put_get_captured(self, ray_cluster: dict[str, str]) -> None:
        run_id = _run_pipeline(self.project_dir, ray_cluster)

        rows = query_roar_db(
            self.project_dir,
            """
            SELECT COALESCE(path, first_seen_path) AS path
            FROM artifacts
            WHERE COALESCE(path, first_seen_path) LIKE ?
            """,
            (f"%/{run_id}/%",),
        )
        assert len(rows) >= 13, f"Expected >=13 distinct S3 artifacts, got {len(rows)}: {rows}"

    def test_cross_task_s3_artifact_identity(self, ray_cluster: dict[str, str]) -> None:
        run_id = _run_pipeline(self.project_dir, ray_cluster)

        rows = query_roar_db(
            self.project_dir,
            """
            SELECT a.id,
                   COALESCE(a.path, a.first_seen_path) AS path,
                   COUNT(DISTINCT jo.job_id) AS producer_count,
                   COUNT(DISTINCT ji.job_id) AS consumer_count
            FROM artifacts a
            LEFT JOIN job_outputs jo ON jo.artifact_id = a.id
            LEFT JOIN job_inputs ji ON ji.artifact_id = a.id
            WHERE COALESCE(a.path, a.first_seen_path) LIKE ?
            GROUP BY a.id
            HAVING producer_count > 0 AND consumer_count > 0
            """,
            (f"%processed/{run_id}/%",),
        )
        assert len(rows) == 3
        for row in rows:
            assert row["producer_count"] == 1, f"{row['path']}: expected 1 producer"
            assert row["consumer_count"] == 1, f"{row['path']}: expected 1 consumer"

    def test_model_artifacts_have_cross_task_identity(self, ray_cluster: dict[str, str]) -> None:
        run_id = _run_pipeline(self.project_dir, ray_cluster)

        rows = query_roar_db(
            self.project_dir,
            """
            SELECT a.id,
                   COALESCE(a.path, a.first_seen_path) AS path,
                   COUNT(DISTINCT jo.job_id) AS producer_count,
                   COUNT(DISTINCT ji.job_id) AS consumer_count
            FROM artifacts a
            LEFT JOIN job_outputs jo ON jo.artifact_id = a.id
            LEFT JOIN job_inputs ji ON ji.artifact_id = a.id
            WHERE COALESCE(a.path, a.first_seen_path) LIKE ?
            GROUP BY a.id
            HAVING producer_count > 0 AND consumer_count > 0
            """,
            (f"%models/{run_id}/%",),
        )
        assert len(rows) == 3
        for row in rows:
            assert row["producer_count"] == 1, f"{row['path']}: expected 1 producer"
            assert row["consumer_count"] == 1, f"{row['path']}: expected 1 consumer"

    def test_no_orphaned_s3_artifacts(self, ray_cluster: dict[str, str]) -> None:
        run_id = _run_pipeline(self.project_dir, ray_cluster)

        rows = query_roar_db(
            self.project_dir,
            """
            SELECT COALESCE(a.path, a.first_seen_path) AS path,
                   COUNT(DISTINCT jo.job_id) AS producers,
                   COUNT(DISTINCT ji.job_id) AS consumers
            FROM artifacts a
            LEFT JOIN job_outputs jo ON jo.artifact_id = a.id
            LEFT JOIN job_inputs ji ON ji.artifact_id = a.id
            WHERE COALESCE(a.path, a.first_seen_path) LIKE ?
               OR COALESCE(a.path, a.first_seen_path) LIKE ?
               OR COALESCE(a.path, a.first_seen_path) LIKE ?
            GROUP BY a.id
            """,
            (
                f"%processed/{run_id}/%",
                f"%models/{run_id}/%",
                f"%metrics/{run_id}/%",
            ),
        )
        assert rows
        for row in rows:
            assert row["producers"] >= 1, f"Orphaned artifact (no producer): {row['path']}"
            assert row["consumers"] >= 1, f"Dangling artifact (no consumer): {row['path']}"

    def test_lineage_depth_reaches_raw_inputs(self, ray_cluster: dict[str, str]) -> None:
        run_id = _run_pipeline(self.project_dir, ray_cluster)

        report_jobs = query_roar_db(
            self.project_dir,
            """
            SELECT j.id
            FROM jobs j
            JOIN job_outputs jo ON jo.job_id = j.id
            JOIN artifacts a ON jo.artifact_id = a.id
            WHERE COALESCE(a.path, a.first_seen_path) LIKE ?
            """,
            (f"%results/{run_id}/final_report.json",),
        )
        assert report_jobs, "No job found that wrote final_report"

        visited_jobs: set[int] = set()
        frontier = {int(report_jobs[0]["id"])}
        depth = 0
        all_paths: set[str] = set()

        while frontier and depth < 8:
            next_frontier: set[int] = set()
            for job_id in frontier:
                if job_id in visited_jobs:
                    continue
                visited_jobs.add(job_id)
                inputs = query_roar_db(
                    self.project_dir,
                    """
                    SELECT a.id,
                           COALESCE(a.path, a.first_seen_path) AS path,
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
                for row in inputs:
                    path = row.get("path")
                    if isinstance(path, str):
                        all_paths.add(path)
                    if row.get("producer_job_id") is not None:
                        next_frontier.add(int(row["producer_job_id"]))
            frontier = next_frontier
            depth += 1

        raw_paths = [path for path in all_paths if f"raw/{run_id}/" in path]
        assert len(raw_paths) == 3, (
            f"Expected 3 raw shard paths in lineage, got {len(raw_paths)}. "
            f"All ancestor paths: {sorted(all_paths)}"
        )
