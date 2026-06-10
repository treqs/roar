from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.backends.ray import collector
from roar.backends.ray.fragment import ArtifactRef, TaskFragment
from roar.db.schema import SCHEMA


def _init_db(project_dir: Path) -> Path:
    db_path = project_dir / ".roar" / "roar.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def test_collect_records_hash_rows_from_fragments(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="driver01",
        ray_task_id="task-abcd1234",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="eval_model",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        reads=[],
        writes=[
            ArtifactRef(
                path="s3://output-bucket/results/run-1/final_report.json",
                hash="etag-final-123",
                hash_algorithm="etag",
                size=123,
                capture_method="proxy",
            )
        ],
    )

    collector.collect(project_dir=str(project_dir), fragments=[fragment.to_dict()])

    conn = sqlite3.connect(db_path)
    hash_row = conn.execute(
        """
        SELECT ah.algorithm, ah.digest
        FROM artifacts a
        JOIN artifact_hashes ah ON ah.artifact_id = a.id
        WHERE a.first_seen_path = ?
        """,
        ("s3://output-bucket/results/run-1/final_report.json",),
    ).fetchone()
    output_row = conn.execute(
        """
        SELECT jo.path
        FROM job_outputs jo
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE a.first_seen_path = ?
        """,
        ("s3://output-bucket/results/run-1/final_report.json",),
    ).fetchone()
    conn.close()

    assert hash_row == ("etag", "etag-final-123")
    assert output_row == ("s3://output-bucket/results/run-1/final_report.json",)


