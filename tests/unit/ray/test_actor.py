from __future__ import annotations

import uuid

import pytest
import ray

from roar.ray.actor import RoarLogCollectorActor


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
