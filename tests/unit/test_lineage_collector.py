"""Unit tests for lineage collector service."""

import sqlite3
from unittest.mock import Mock

from roar.db.schema import SCHEMA, run_migrations
from roar.services.upload.lineage_collector import (
    LineageCollector,
    _extract_primary_digest,
    compute_io_signature,
)


class TestComputeIoSignature:
    """Tests for compute_io_signature function."""

    def test_empty_io_uses_job_uid(self):
        """Jobs with empty I/O should use job_uid as signature."""
        job = {"id": 1, "job_uid": "abc123", "_input_hashes": [], "_output_hashes": []}
        sig = compute_io_signature(job)
        assert sig == "unique:abc123"

    def test_empty_io_falls_back_to_id(self):
        """Jobs with empty I/O and no job_uid should fall back to id."""
        job = {"id": 42, "_input_hashes": [], "_output_hashes": []}
        sig = compute_io_signature(job)
        assert sig == "unique:42"

    def test_jobs_with_io_use_hash_signature(self):
        """Jobs with I/O should use hash-based signature."""
        job = {"id": 1, "job_uid": "abc", "_input_hashes": ["hash1"], "_output_hashes": ["hash2"]}
        sig = compute_io_signature(job)
        assert sig == "('hash1',)|('hash2',)"

    def test_signature_sorts_hashes(self):
        """Hashes should be sorted for consistent signatures."""
        job = {"id": 1, "_input_hashes": ["z", "a", "m"], "_output_hashes": ["b", "a"]}
        sig = compute_io_signature(job)
        assert sig == "('a', 'm', 'z')|('a', 'b')"


class TestDeduplicateReruns:
    """Tests for _deduplicate_reruns method."""

    def test_preserves_build_jobs_with_empty_io(self):
        """Build jobs with empty I/O should not be deduplicated."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 1,
                "job_uid": "b1",
                "job_type": "build",
                "timestamp": 1.0,
                "_input_hashes": [],
                "_output_hashes": [],
            },
            {
                "id": 2,
                "job_uid": "b2",
                "job_type": "build",
                "timestamp": 2.0,
                "_input_hashes": [],
                "_output_hashes": [],
            },
            {
                "id": 3,
                "job_uid": "b3",
                "job_type": "build",
                "timestamp": 3.0,
                "_input_hashes": [],
                "_output_hashes": [],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert len(result) == 3

    def test_still_deduplicates_run_jobs_with_io(self):
        """Run jobs with same I/O should still be deduplicated."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 1,
                "job_uid": "r1",
                "timestamp": 1.0,
                "_input_hashes": ["abc"],
                "_output_hashes": ["def"],
            },
            {
                "id": 2,
                "job_uid": "r2",
                "timestamp": 2.0,
                "_input_hashes": ["abc"],
                "_output_hashes": ["def"],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert len(result) == 1
        assert result[0]["job_uid"] == "r2"  # Latest kept

    def test_keeps_jobs_with_different_io(self):
        """Jobs with different I/O should not be deduplicated."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 1,
                "job_uid": "r1",
                "timestamp": 1.0,
                "_input_hashes": ["abc"],
                "_output_hashes": ["def"],
            },
            {
                "id": 2,
                "job_uid": "r2",
                "timestamp": 2.0,
                "_input_hashes": ["xyz"],
                "_output_hashes": ["uvw"],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert len(result) == 2

    def test_results_sorted_by_timestamp(self):
        """Deduplicated results should be sorted by timestamp."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 3,
                "job_uid": "j3",
                "timestamp": 3.0,
                "_input_hashes": ["c"],
                "_output_hashes": ["c"],
            },
            {
                "id": 1,
                "job_uid": "j1",
                "timestamp": 1.0,
                "_input_hashes": ["a"],
                "_output_hashes": ["a"],
            },
            {
                "id": 2,
                "job_uid": "j2",
                "timestamp": 2.0,
                "_input_hashes": ["b"],
                "_output_hashes": ["b"],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert [j["id"] for j in result] == [1, 2, 3]


class TestCompositeDigestHandling:
    """Coverage for composite-aware digest extraction and lookup."""

    def test_extract_primary_digest_prefers_blake3(self):
        digest = _extract_primary_digest(
            {
                "hashes": [
                    {"algorithm": "composite-blake3", "digest": "c" * 64},
                    {"algorithm": "blake3", "digest": "b" * 64},
                ]
            }
        )
        assert digest == "b" * 64

    def test_extract_primary_digest_uses_composite_blake3_when_needed(self):
        digest = _extract_primary_digest(
            {"hashes": [{"algorithm": "composite-blake3", "digest": "c" * 64}]}
        )
        assert digest == "c" * 64

    def test_build_job_io_keeps_composite_hashes(self):
        collector = LineageCollector()
        ctx_db = Mock()
        ctx_db.jobs.get_latest_build_jobs.return_value = [
            {"id": 42, "job_uid": "build-42", "timestamp": 1.0}
        ]
        ctx_db.jobs.get_inputs.return_value = [
            {
                "hashes": [{"algorithm": "composite-blake3", "digest": "a" * 64}],
                "path": "/tmp/dataset-in",
                "first_seen_path": "/tmp/dataset-in",
                "byte_ranges": None,
            }
        ]
        ctx_db.jobs.get_outputs.return_value = [
            {
                "hashes": [{"algorithm": "composite-blake3", "digest": "b" * 64}],
                "path": "/tmp/dataset-out",
                "first_seen_path": "/tmp/dataset-out",
                "byte_ranges": None,
            }
        ]

        jobs = collector._add_build_jobs(
            ctx_db=ctx_db,
            pipeline={"id": 1},
            lineage_jobs=[],
            lineage_artifact_hashes=set(),
        )
        assert jobs[0]["_input_hashes"] == ["a" * 64]
        assert jobs[0]["_output_hashes"] == ["b" * 64]
        assert jobs[0]["_inputs"][0]["path"] == "/tmp/dataset-in"
        assert jobs[0]["_outputs"][0]["path"] == "/tmp/dataset-out"

    def test_artifact_lookup_falls_back_to_composite_algorithm(self):
        collector = LineageCollector()
        ctx_db = Mock()

        def lookup(hash_value: str, algorithm: str | None = None):
            if algorithm == "blake3":
                return None
            if algorithm == "composite-blake3":
                return {
                    "id": "artifact-composite",
                    "hashes": [{"algorithm": "composite-blake3", "digest": hash_value}],
                    "size": 1,
                }
            return None

        ctx_db.artifacts.get_by_hash.side_effect = lookup
        composite_hash = "d" * 64
        artifacts = collector._get_artifact_info(ctx_db, {composite_hash})

        assert len(artifacts) == 1
        assert artifacts[0]["hash"] == composite_hash
        assert ctx_db.artifacts.get_by_hash.call_args_list[0].kwargs == {"algorithm": "blake3"}
        assert ctx_db.artifacts.get_by_hash.call_args_list[1].kwargs == {
            "algorithm": "composite-blake3"
        }


def test_collect_includes_ray_task_jobs_with_parent_links(tmp_path):
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(parents=True, exist_ok=True)
    db_path = roar_dir / "roar.db"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    run_migrations(conn)

    artifact_rows = [
        ("art-task-1", "a" * 64, "/tmp/task1.ckpt"),
        ("art-task-2", "b" * 64, "/tmp/task2.ckpt"),
        ("art-driver", "c" * 64, "/tmp/final-model.pt"),
    ]
    for artifact_id, digest, path in artifact_rows:
        conn.execute(
            """
            INSERT INTO artifacts (id, size, first_seen_at, first_seen_path, path, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, 1, 1.0, path, path, "primitive"),
        )
        conn.execute(
            """
            INSERT INTO artifact_hashes (artifact_id, algorithm, digest)
            VALUES (?, ?, ?)
            """,
            (artifact_id, "blake3", digest),
        )

    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task1111", 1.0, "ray_task:process", "ray_task", "driver01"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task2222", 1.1, "ray_task:process", "ray_task", "driver01"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("driver01", 2.0, "python train.py", None, None),
    )

    job_ids = {
        row["job_uid"]: row["id"] for row in conn.execute("SELECT id, job_uid FROM jobs").fetchall()
    }

    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task1111"], "art-task-1", "/tmp/task1.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task2222"], "art-task-2", "/tmp/task2.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["driver01"], "art-driver", "/tmp/final-model.pt"),
    )
    conn.execute(
        "INSERT INTO job_inputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["driver01"], "art-task-1", "/tmp/task1.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_inputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["driver01"], "art-task-2", "/tmp/task2.ckpt"),
    )
    conn.commit()
    conn.close()

    lineage = LineageCollector().collect(["c" * 64], roar_dir)
    jobs_by_uid = {job["job_uid"]: job for job in lineage.jobs}

    assert {"driver01", "task1111", "task2222"} <= set(jobs_by_uid)
    assert jobs_by_uid["task1111"]["job_type"] == "ray_task"
    assert jobs_by_uid["task2222"]["job_type"] == "ray_task"
    assert jobs_by_uid["task1111"]["parent_job_uid"] == "driver01"
    assert jobs_by_uid["task2222"]["parent_job_uid"] == "driver01"


def test_collect_includes_parent_linked_ray_tasks_without_driver_input_edges(tmp_path):
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(parents=True, exist_ok=True)
    db_path = roar_dir / "roar.db"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    run_migrations(conn)

    artifact_rows = [
        ("art-task-1", "1" * 64, "/tmp/task1.ckpt"),
        ("art-task-2", "2" * 64, "/tmp/task2.ckpt"),
        ("art-task-3", "3" * 64, "/tmp/task3.ckpt"),
        ("art-driver", "4" * 64, "/tmp/model.json"),
    ]
    for artifact_id, digest, path in artifact_rows:
        conn.execute(
            """
            INSERT INTO artifacts (id, size, first_seen_at, first_seen_path, path, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, 1, 1.0, path, path, "primitive"),
        )
        conn.execute(
            """
            INSERT INTO artifact_hashes (artifact_id, algorithm, digest)
            VALUES (?, ?, ?)
            """,
            (artifact_id, "blake3", digest),
        )

    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-a", 1.0, "ray_task:process", "ray_task", "driver-main"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-b", 1.1, "ray_task:process", "ray_task", "driver-main"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-c", 1.2, "ray_task:process", "ray_task", "driver-main"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("driver-main", 2.0, "python train.py", None, None),
    )

    job_ids = {
        row["job_uid"]: row["id"] for row in conn.execute("SELECT id, job_uid FROM jobs").fetchall()
    }

    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task-a"], "art-task-1", "/tmp/task1.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task-b"], "art-task-2", "/tmp/task2.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task-c"], "art-task-3", "/tmp/task3.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["driver-main"], "art-driver", "/tmp/model.json"),
    )
    conn.commit()
    conn.close()

    lineage = LineageCollector().collect(["4" * 64], roar_dir)
    jobs_by_uid = {job["job_uid"]: job for job in lineage.jobs}

    assert {"driver-main", "task-a", "task-b", "task-c"} <= set(jobs_by_uid)
    assert jobs_by_uid["task-a"]["job_type"] == "ray_task"
    assert jobs_by_uid["task-b"]["job_type"] == "ray_task"
    assert jobs_by_uid["task-c"]["job_type"] == "ray_task"
    assert jobs_by_uid["task-a"]["parent_job_uid"] == "driver-main"
    assert jobs_by_uid["task-b"]["parent_job_uid"] == "driver-main"
    assert jobs_by_uid["task-c"]["parent_job_uid"] == "driver-main"


def test_collect_task_output_includes_parent_and_sibling_ray_tasks(tmp_path):
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(parents=True, exist_ok=True)
    db_path = roar_dir / "roar.db"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    run_migrations(conn)

    artifact_rows = [
        ("art-task-1", "a" * 64, "/tmp/task1.ckpt"),
        ("art-task-2", "b" * 64, "/tmp/task2.ckpt"),
        ("art-task-3", "c" * 64, "/tmp/task3.ckpt"),
    ]
    for artifact_id, digest, path in artifact_rows:
        conn.execute(
            """
            INSERT INTO artifacts (id, size, first_seen_at, first_seen_path, path, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, 1, 1.0, path, path, "primitive"),
        )
        conn.execute(
            """
            INSERT INTO artifact_hashes (artifact_id, algorithm, digest)
            VALUES (?, ?, ?)
            """,
            (artifact_id, "blake3", digest),
        )

    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("driver-main", 1.0, "python pipeline.py", None, None),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-a", 1.1, "ray_task:ingest", "ray_task", "driver-main"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-b", 1.2, "ray_task:train", "ray_task", "driver-main"),
    )
    conn.execute(
        """
        INSERT INTO jobs (job_uid, timestamp, command, job_type, parent_job_uid)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-c", 1.3, "ray_task:eval", "ray_task", "driver-main"),
    )

    job_ids = {
        row["job_uid"]: row["id"] for row in conn.execute("SELECT id, job_uid FROM jobs").fetchall()
    }
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task-a"], "art-task-1", "/tmp/task1.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task-b"], "art-task-2", "/tmp/task2.ckpt"),
    )
    conn.execute(
        "INSERT INTO job_outputs (job_id, artifact_id, path) VALUES (?, ?, ?)",
        (job_ids["task-c"], "art-task-3", "/tmp/task3.ckpt"),
    )
    conn.commit()
    conn.close()

    lineage = LineageCollector().collect(["a" * 64], roar_dir)
    jobs_by_uid = {job["job_uid"]: job for job in lineage.jobs}

    assert {"driver-main", "task-a", "task-b", "task-c"} <= set(jobs_by_uid)
    assert jobs_by_uid["task-a"]["parent_job_uid"] == "driver-main"
    assert jobs_by_uid["task-b"]["parent_job_uid"] == "driver-main"
    assert jobs_by_uid["task-c"]["parent_job_uid"] == "driver-main"
