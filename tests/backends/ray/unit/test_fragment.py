from __future__ import annotations

from roar.backends.ray.fragment import (
    ArtifactRef,
    TaskFragment,
    derive_task_identity,
    derive_task_uid,
)


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
        backend_metadata={"execution_role": "worker", "world_rank": 0},
    )


def test_task_fragment_round_trip_dict() -> None:
    fragment = _sample_fragment()

    as_dict = fragment.to_dict()
    restored = TaskFragment.from_dict(as_dict)

    assert restored == fragment


def test_derive_task_uid_is_deterministic() -> None:
    uid1 = derive_task_uid("job-a", "task-1")
    uid2 = derive_task_uid("job-a", "task-1")

    assert uid1 == uid2


def test_derive_task_uid_changes_with_task_id() -> None:
    uid1 = derive_task_uid("job-a", "task-1")
    uid2 = derive_task_uid("job-a", "task-2")

    assert uid1 != uid2


def test_derive_task_identity_changes_with_job_uid_when_parent_is_missing() -> None:
    identity_one = derive_task_identity("", "task-1", "deadbeef")
    identity_two = derive_task_identity("", "task-1", "feedface")

    assert identity_one != identity_two


def test_task_fragment_populates_task_identity_from_parent_and_ray_task_id() -> None:
    fragment = _sample_fragment()

    assert fragment.task_identity == derive_task_identity("cafebabe", "task-123", "deadbeef")


def test_task_fragment_populates_deterministic_identity_when_primary_ids_missing() -> None:
    fragment_one = TaskFragment(
        job_uid="",
        parent_job_uid="",
        ray_task_id="",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train_step",
        started_at=100.0,
        ended_at=120.0,
        exit_code=0,
        writes=[
            ArtifactRef(
                path="/tmp/output.parquet",
                hash="b" * 64,
                hash_algorithm="blake3",
                size=84,
                capture_method="proxy",
            )
        ],
    )
    fragment_two = TaskFragment.from_dict(fragment_one.to_dict())

    assert fragment_one.task_identity
    assert fragment_two.task_identity == fragment_one.task_identity
