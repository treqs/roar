from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.db.schema import SCHEMA
from roar.ray.collector import collect_fragments
from roar.ray.fragment import ArtifactRef, TaskFragment


def _init_db(project_dir: Path) -> Path:
    db_path = project_dir / ".roar" / "roar.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def test_collect_fragments_writes_task_jobs_and_deduplicates_artifacts(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    shared_digest = "a" * 64
    input_1_digest = "b" * 64
    input_2_digest = "c" * 64

    fragment_one = TaskFragment(
        job_uid="11111111",
        parent_job_uid="abc",
        ray_task_id="task-1",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        reads=[
            ArtifactRef(
                path="/tmp/input-1.bin",
                hash=input_1_digest,
                hash_algorithm="blake3",
                size=10,
                capture_method="python",
            )
        ],
        writes=[
            ArtifactRef(
                path="/tmp/shared-output.bin",
                hash=shared_digest,
                hash_algorithm="blake3",
                size=20,
                capture_method="python",
            )
        ],
    )

    fragment_two = TaskFragment(
        job_uid="22222222",
        parent_job_uid="abc",
        ray_task_id="task-2",
        ray_worker_id="worker-2",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=3.0,
        ended_at=4.0,
        exit_code=0,
        reads=[
            ArtifactRef(
                path="/tmp/input-2.bin",
                hash=input_2_digest,
                hash_algorithm="blake3",
                size=11,
                capture_method="python",
            )
        ],
        writes=[
            ArtifactRef(
                path="/tmp/shared-output.bin",
                hash=shared_digest,
                hash_algorithm="blake3",
                size=20,
                capture_method="python",
            )
        ],
    )

    collect_fragments(
        fragments=[fragment_one.to_dict(), fragment_two.to_dict()],
        project_dir=str(project_dir),
        driver_job_uid="abc",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    jobs = conn.execute(
        """
        SELECT job_uid, job_type, parent_job_uid
        FROM jobs
        WHERE job_type = 'ray_task'
        ORDER BY job_uid
        """
    ).fetchall()
    assert len(jobs) == 2
    assert [row["job_uid"] for row in jobs] == ["11111111", "22222222"]
    assert {row["parent_job_uid"] for row in jobs} == {"abc"}

    shared_hash_rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifact_hashes
        WHERE algorithm = 'blake3' AND digest = ?
        """,
        (shared_digest,),
    ).fetchall()
    assert len(shared_hash_rows) == 1
    shared_artifact_id = shared_hash_rows[0]["artifact_id"]

    output_rows = conn.execute(
        """
        SELECT jo.job_id, jo.artifact_id, jo.path
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE j.job_type = 'ray_task'
        ORDER BY jo.job_id
        """
    ).fetchall()
    assert len(output_rows) == 2
    assert {row["artifact_id"] for row in output_rows} == {shared_artifact_id}
    assert {row["path"] for row in output_rows} == {"/tmp/shared-output.bin"}

    input_rows = conn.execute(
        """
        SELECT ji.job_id, ah.digest
        FROM job_inputs ji
        JOIN artifact_hashes ah ON ah.artifact_id = ji.artifact_id
        WHERE ah.algorithm = 'blake3'
        """
    ).fetchall()
    assert len(input_rows) == 2
    assert {row["digest"] for row in input_rows} == {input_1_digest, input_2_digest}

    artifact_rows = conn.execute("SELECT id FROM artifacts").fetchall()
    assert len(artifact_rows) == 3

    conn.close()
