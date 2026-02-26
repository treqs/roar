from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.db.schema import SCHEMA
from roar.ray import collector as ray_collector
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


def _make_fragment(job_uid: str, started_at: float, function_name: str = "task") -> TaskFragment:
    return TaskFragment(
        job_uid=job_uid,
        parent_job_uid="abc",
        ray_task_id=f"task-{job_uid}",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name=function_name,
        started_at=started_at,
        ended_at=started_at + 0.5,
        exit_code=0,
    )


def test_assign_step_numbers_groups_fragments_within_one_second_window() -> None:
    fragments = [
        _make_fragment("a1111111", 0.0),
        _make_fragment("b2222222", 0.2),
        _make_fragment("c3333333", 0.8),
    ]

    step_map = ray_collector._assign_step_numbers(fragments)

    assert step_map == {"a1111111": 2, "b2222222": 2, "c3333333": 2}


def test_assign_step_numbers_creates_new_group_when_more_than_one_second_apart() -> None:
    fragments = [
        _make_fragment("a1111111", 0.0),
        _make_fragment("b2222222", 0.6),
        _make_fragment("c3333333", 2.5),
    ]

    step_map = ray_collector._assign_step_numbers(fragments)

    assert step_map == {"a1111111": 2, "b2222222": 2, "c3333333": 3}


def test_assign_step_numbers_sequential_pipeline_steps() -> None:
    fragments = [
        _make_fragment("ingest01", 0.0),
        _make_fragment("train002", 5.0),
        _make_fragment("eval0003", 10.0),
    ]

    step_map = ray_collector._assign_step_numbers(fragments)

    assert step_map == {"ingest01": 2, "train002": 3, "eval0003": 4}


def test_assign_step_numbers_single_fragment() -> None:
    fragments = [_make_fragment("single01", 7.0)]

    step_map = ray_collector._assign_step_numbers(fragments)

    assert step_map == {"single01": 2}


def test_assign_step_numbers_empty_fragments() -> None:
    assert ray_collector._assign_step_numbers([]) == {}


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


def test_collect_fragments_persists_artifact_size_from_fragment_refs(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment = TaskFragment(
        job_uid="33333333",
        parent_job_uid="abc",
        ray_task_id="task-3",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="write",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[
            ArtifactRef(
                path="s3://demo-bucket/path/to/output.bin",
                hash="etag-123",
                hash_algorithm="etag",
                size=123,
                capture_method="proxy",
            )
        ],
    )

    collect_fragments(
        fragments=[fragment.to_dict()],
        project_dir=str(project_dir),
        driver_job_uid="abc",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT size
        FROM artifacts
        WHERE first_seen_path = ?
        ORDER BY first_seen_at DESC
        LIMIT 1
        """,
        ("s3://demo-bucket/path/to/output.bin",),
    ).fetchone()
    assert row is not None
    assert int(row["size"]) == 123
    conn.close()


def test_collect_fragments_assigns_step_numbers_from_fragment_timestamps(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragments = [
        _make_fragment("ingest01", 0.0, function_name="ingest"),
        _make_fragment("train002", 5.0, function_name="train"),
        _make_fragment("eval0003", 10.0, function_name="eval"),
    ]

    collect_fragments(
        fragments=[fragment.to_dict() for fragment in fragments],
        project_dir=str(project_dir),
        driver_job_uid="abc",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT job_uid, step_number
        FROM jobs
        WHERE job_type = 'ray_task'
        """
    ).fetchall()
    conn.close()

    step_map = {row["job_uid"]: row["step_number"] for row in rows}
    assert step_map == {"ingest01": 2, "train002": 3, "eval0003": 4}
