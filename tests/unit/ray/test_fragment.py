from __future__ import annotations

from roar.ray.fragment import ArtifactRef, TaskFragment, derive_task_uid


def _sample_fragment() -> TaskFragment:
    return TaskFragment(
        job_uid="deadbeef",
        parent_job_uid="cafebabe",
        ray_task_id="task-123",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train_step",
        started_at=100.0,
        ended_at=120.0,
        exit_code=0,
        reads=[
            ArtifactRef(
                path="/tmp/input.parquet",
                hash="a" * 64,
                hash_algorithm="blake3",
                size=42,
                capture_method="python",
            )
        ],
        writes=[
            ArtifactRef(
                path="/tmp/output.parquet",
                hash="b" * 64,
                hash_algorithm="blake3",
                size=84,
                capture_method="proxy",
            )
        ],
        worker_packages={"ray": "2.0.0"},
    )


def test_task_fragment_round_trip_dict() -> None:
    fragment = _sample_fragment()

    as_dict = fragment.to_dict()
    restored = TaskFragment.from_dict(as_dict)

    assert restored == fragment


def test_artifact_ref_fields() -> None:
    artifact = ArtifactRef(
        path="s3://bucket/key",
        hash="etag-123",
        hash_algorithm="etag",
        size=123,
        capture_method="proxy",
    )

    assert artifact.path == "s3://bucket/key"
    assert artifact.hash == "etag-123"
    assert artifact.hash_algorithm == "etag"
    assert artifact.size == 123
    assert artifact.capture_method == "proxy"


def test_derive_task_uid_is_deterministic() -> None:
    uid1 = derive_task_uid("job-a", "task-1")
    uid2 = derive_task_uid("job-a", "task-1")

    assert uid1 == uid2


def test_derive_task_uid_changes_with_task_id() -> None:
    uid1 = derive_task_uid("job-a", "task-1")
    uid2 = derive_task_uid("job-a", "task-2")

    assert uid1 != uid2


def test_derive_task_uid_is_8_char_hex() -> None:
    uid = derive_task_uid("job-a", "task-1")

    assert len(uid) == 8
    assert int(uid, 16) >= 0
