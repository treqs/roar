from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.backends.ray import collector as ray_collector
from roar.backends.ray.collector import collect_fragments
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


def _ref(hash_value: str) -> ArtifactRef:
    return ArtifactRef(
        path=f"/workspace/{hash_value}",
        hash=hash_value,
        hash_algorithm="blake3",
        size=0,
        capture_method="python",
    )


def _make_fragment(
    job_uid: str,
    started_at: float,
    function_name: str = "task",
    reads: tuple[str, ...] = (),
    writes: tuple[str, ...] = (),
) -> TaskFragment:
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
        reads=[_ref(hash_value) for hash_value in reads],
        writes=[_ref(hash_value) for hash_value in writes],
    )


def test_assign_step_numbers_uses_dependency_depth_not_time_window() -> None:
    fragments = [
        _make_fragment("a1111111", 0.0, writes=("h1",)),
        _make_fragment("b2222222", 0.2, reads=("h1",), writes=("h2",)),
        _make_fragment("c3333333", 0.8, reads=("h2",)),
    ]

    step_map = ray_collector._assign_step_numbers(fragments)

    assert step_map == {"a1111111": 2, "b2222222": 3, "c3333333": 4}


def test_assign_step_numbers_ignores_timestamp_gaps_without_dependencies() -> None:
    fragments = [
        _make_fragment("a1111111", 0.0),
        _make_fragment("b2222222", 0.6),
        _make_fragment("c3333333", 2.5),
    ]

    step_map = ray_collector._assign_step_numbers(fragments)

    assert step_map == {"a1111111": 2, "b2222222": 2, "c3333333": 2}


def test_assign_step_numbers_sequential_pipeline_steps() -> None:
    fragments = [
        _make_fragment("ingest01", 5.0, writes=("processed",)),
        _make_fragment("train002", 5.0, reads=("processed",), writes=("model",)),
        _make_fragment("eval0003", 5.0, reads=("model",)),
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
                path="/workspace/input-1.bin",
                hash=input_1_digest,
                hash_algorithm="blake3",
                size=10,
                capture_method="python",
            )
        ],
        writes=[
            ArtifactRef(
                path="/workspace/shared-output.bin",
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
                path="/workspace/input-2.bin",
                hash=input_2_digest,
                hash_algorithm="blake3",
                size=11,
                capture_method="python",
            )
        ],
        writes=[
            ArtifactRef(
                path="/workspace/shared-output.bin",
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
    assert {row["path"] for row in output_rows} == {"/workspace/shared-output.bin"}

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


def test_collect_fragments_shortens_command_to_task_family_and_keeps_full_script(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment = TaskFragment(
        job_uid="shortcmd1",
        parent_job_uid="abc",
        ray_task_id="task-short",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="cloud_demo_like.workload.extract_shard",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[
            ArtifactRef(
                path="s3://demo-bucket/output.json",
                hash="etag-1",
                hash_algorithm="etag",
                size=1,
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
        SELECT command, script
        FROM jobs
        WHERE job_uid = ?
        """,
        (fragment.job_uid,),
    ).fetchone()
    assert row is not None
    assert row["command"] == "ray_task:extract_shard"
    assert row["script"] == "cloud_demo_like.workload.extract_shard"
    conn.close()


def test_collect_fragments_assigns_step_numbers_from_fragment_dependencies(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragments = [
        _make_fragment("ingest01", 1.0, function_name="ingest", writes=("proc",)),
        _make_fragment(
            "train002",
            1.0,
            function_name="train",
            reads=("proc",),
            writes=("model",),
        ),
        _make_fragment("eval0003", 1.0, function_name="eval", reads=("model",)),
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


def test_collect_fragments_is_idempotent_when_fragment_reads_and_writes_same_path(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment = TaskFragment(
        job_uid="same-path-1",
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
                path="/workspace/data.json",
                hash=None,
                hash_algorithm="blake3",
                size=0,
                capture_method="python",
            )
        ],
        writes=[
            ArtifactRef(
                path="/workspace/data.json",
                hash="d" * 64,
                hash_algorithm="blake3",
                size=12,
                capture_method="python",
            )
        ],
    )

    payload = [fragment.to_dict()]
    collect_fragments(
        fragments=payload,
        project_dir=str(project_dir),
        driver_job_uid="abc",
    )
    collect_fragments(
        fragments=payload,
        project_dir=str(project_dir),
        driver_job_uid="abc",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT ji.artifact_id AS input_artifact_id, jo.artifact_id AS output_artifact_id
            FROM job_inputs ji
            JOIN job_outputs jo ON jo.job_id = ji.job_id AND jo.path = ji.path
            JOIN jobs j ON j.id = ji.job_id
            WHERE j.job_uid = ?
            """,
            ("same-path-1",),
        ).fetchone()
        counts = conn.execute("SELECT COUNT(*) AS count FROM job_inputs").fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["input_artifact_id"] == row["output_artifact_id"]
    assert int(counts["count"]) == 1


def test_collect_fragments_keeps_distinct_tasks_when_job_uid_collides(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment_one = TaskFragment(
        job_uid="deadbeef",
        parent_job_uid="driver-main",
        ray_task_id="task-1",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[_ref("a" * 64)],
    )
    fragment_two = TaskFragment(
        job_uid="deadbeef",
        parent_job_uid="driver-main",
        ray_task_id="task-2",
        ray_worker_id="worker-2",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=3.0,
        ended_at=4.0,
        exit_code=0,
        writes=[_ref("b" * 64)],
    )

    collect_fragments(
        fragments=[fragment_one.to_dict(), fragment_two.to_dict()],
        project_dir=str(project_dir),
        driver_job_uid="driver-main",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                job_uid,
                json_extract(metadata, '$.ray_task_id') AS ray_task_id,
                json_extract(metadata, '$.task_identity') AS task_identity
            FROM jobs
            WHERE job_type = 'ray_task'
            ORDER BY ray_task_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert {row["ray_task_id"] for row in rows} == {"task-1", "task-2"}
    assert len({row["job_uid"] for row in rows}) == 2
    assert len({row["task_identity"] for row in rows}) == 2


def test_collect_fragments_reuses_existing_task_identity_when_job_uid_changes(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    legacy_fragment = TaskFragment(
        job_uid="deadbeef",
        parent_job_uid="driver-main",
        ray_task_id="task-1",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[_ref("a" * 64)],
    )
    stronger_uid_fragment = TaskFragment(
        job_uid="0123456789abcdef0123456789abcdef",
        parent_job_uid="driver-main",
        ray_task_id="task-1",
        ray_worker_id="worker-2",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=3.0,
        ended_at=4.0,
        exit_code=0,
        writes=[_ref("b" * 64)],
    )

    collect_fragments(
        fragments=[legacy_fragment.to_dict()],
        project_dir=str(project_dir),
        driver_job_uid="driver-main",
    )
    collect_fragments(
        fragments=[stronger_uid_fragment.to_dict()],
        project_dir=str(project_dir),
        driver_job_uid="driver-main",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                job_uid,
                json_extract(metadata, '$.ray_task_id') AS ray_task_id,
                json_extract(metadata, '$.task_identity') AS task_identity
            FROM jobs
            WHERE job_type = 'ray_task'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["job_uid"] == "deadbeef"
    assert rows[0]["ray_task_id"] == "task-1"
    assert rows[0]["task_identity"] == legacy_fragment.task_identity


def test_collect_fragments_keeps_distinct_legacy_tasks_when_parent_job_uid_missing(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment_one = TaskFragment(
        job_uid="deadbeef",
        parent_job_uid="",
        ray_task_id="task-1",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[_ref("a" * 64)],
    )
    fragment_two = TaskFragment(
        job_uid="feedface",
        parent_job_uid="",
        ray_task_id="task-1",
        ray_worker_id="worker-2",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=3.0,
        ended_at=4.0,
        exit_code=0,
        writes=[_ref("b" * 64)],
    )

    collect_fragments(
        fragments=[fragment_one.to_dict(), fragment_two.to_dict()],
        project_dir=str(project_dir),
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                job_uid,
                json_extract(metadata, '$.ray_task_id') AS ray_task_id,
                json_extract(metadata, '$.task_identity') AS task_identity
            FROM jobs
            WHERE job_type = 'ray_task'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert {row["job_uid"] for row in rows} == {"deadbeef", "feedface"}
    assert {row["ray_task_id"] for row in rows} == {"task-1"}
    assert len({row["task_identity"] for row in rows}) == 2


def test_collect_fragments_keeps_distinct_tasks_when_identity_is_missing(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment_one = TaskFragment(
        job_uid="",
        parent_job_uid="",
        ray_task_id="",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[_ref("a" * 64)],
    )
    fragment_two = TaskFragment(
        job_uid="",
        parent_job_uid="",
        ray_task_id="",
        ray_worker_id="worker-2",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=3.0,
        ended_at=4.0,
        exit_code=0,
        writes=[_ref("b" * 64)],
    )

    collect_fragments(
        fragments=[fragment_one.to_dict(), fragment_two.to_dict()],
        project_dir=str(project_dir),
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                job_uid,
                json_extract(metadata, '$.task_identity') AS task_identity
            FROM jobs
            WHERE job_type = 'ray_task'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert len({row["job_uid"] for row in rows}) == 2
    assert len({row["task_identity"] for row in rows}) == 2


def test_collect_fragments_reuses_identity_less_fragment_when_payload_repeats(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)

    fragment = TaskFragment(
        job_uid="",
        parent_job_uid="",
        ray_task_id="",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="process",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        writes=[_ref("a" * 64)],
    )

    collect_fragments(
        fragments=[fragment.to_dict()],
        project_dir=str(project_dir),
    )
    collect_fragments(
        fragments=[fragment.to_dict()],
        project_dir=str(project_dir),
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                job_uid,
                json_extract(metadata, '$.task_identity') AS task_identity
            FROM jobs
            WHERE job_type = 'ray_task'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["job_uid"]
    assert rows[0]["task_identity"]


def test_normalize_reconstituted_path_restores_ray_working_dir_to_project_dir(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    packaged_path = (
        "/tmp/ray/session_2026-01-01_00-00-00_000000_0/"
        "runtime_resources/working_dir_files/_ray_pkg_deadbeef/artifacts/output.json"
    )

    normalized = ray_collector._normalize_reconstituted_path(
        packaged_path,
        project_dir=str(project_dir),
    )

    assert normalized == str((project_dir / "artifacts" / "output.json").resolve(strict=False))
