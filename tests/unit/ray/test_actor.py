from __future__ import annotations

import uuid

import pytest

ray = pytest.importorskip("ray")

from roar.ray.actor import RoarLogCollectorActor  # noqa: E402


@pytest.fixture
def ray_runtime() -> None:
    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        namespace=f"roar-test-{uuid.uuid4().hex[:8]}",
        ignore_reinit_error=True,
        include_dashboard=False,
        num_cpus=1,
        log_to_driver=False,
    )
    try:
        yield
    finally:
        ray.shutdown()


def test_actor_append_batch_and_get_all(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    ray.get(
        actor.append_batch.remote(
            [
                {"task_id": "task-1", "path": "/tmp/in.csv", "mode": "r"},
                {"task_id": "task-1", "path": "/tmp/out.csv", "mode": "w"},
            ]
        )
    )

    events = ray.get(actor.get_all.remote())
    assert events == [
        {"task_id": "task-1", "path": "/tmp/in.csv", "mode": "r"},
        {"task_id": "task-1", "path": "/tmp/out.csv", "mode": "w"},
    ]


def test_actor_supports_concurrent_appends(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    refs = [
        actor.append_batch.remote([{"task_id": f"task-{index}", "seq": index}])
        for index in range(200)
    ]
    ray.get(refs)

    events = ray.get(actor.get_all.remote())
    assert len(events) == 200
    assert {event["seq"] for event in events} == set(range(200))


def test_actor_get_all_fragments_is_empty_by_default(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    fragments = ray.get(actor.get_all_fragments.remote())
    assert fragments == []


def test_actor_append_fragment_and_get_all_fragments(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    ray.get(
        actor.append_fragment.remote(
            {
                "job_uid": "abcd1234",
                "parent_job_uid": "deadbeef",
                "ray_task_id": "task-1",
            }
        )
    )
    ray.get(
        actor.append_fragment.remote(
            {
                "job_uid": "abcd5678",
                "parent_job_uid": "deadbeef",
                "ray_task_id": "task-2",
            }
        )
    )

    fragments = ray.get(actor.get_all_fragments.remote())
    assert fragments == [
        {"job_uid": "abcd1234", "parent_job_uid": "deadbeef", "ray_task_id": "task-1"},
        {"job_uid": "abcd5678", "parent_job_uid": "deadbeef", "ray_task_id": "task-2"},
    ]


def test_actor_fragment_and_event_apis_coexist(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    ray.get(actor.append_batch.remote([{"task_id": "task-1", "path": "/tmp/in.csv", "mode": "r"}]))
    ray.get(actor.append_fragment.remote({"job_uid": "feedcafe", "ray_task_id": "task-1"}))

    events = ray.get(actor.get_all.remote())
    fragments = ray.get(actor.get_all_fragments.remote())

    assert events == [{"task_id": "task-1", "path": "/tmp/in.csv", "mode": "r"}]
    assert fragments == [{"job_uid": "feedcafe", "ray_task_id": "task-1"}]


def test_actor_flush_to_glaas_is_true_without_streamer(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    assert ray.get(actor.flush_to_glaas.remote()) is True


def test_actor_streamer_config_keeps_in_memory_fragments(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote(
        session_id="session-123",
        token="ab" * 32,
        glaas_url="http://localhost:3001",
    )

    ray.get(actor.append_fragment.remote({"job_uid": "feedcafe", "ray_task_id": "task-1"}))

    fragments = ray.get(actor.get_all_fragments.remote())
    assert fragments == [{"job_uid": "feedcafe", "ray_task_id": "task-1"}]
