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


def test_actor_ping(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()
    assert ray.get(actor.ping.remote()) is True


def test_actor_append_batch_is_noop(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()

    result = ray.get(
        actor.append_batch.remote(
            [
                {"task_id": "task-1", "path": "/tmp/in.csv", "mode": "r"},
                {"task_id": "task-1", "path": "/tmp/out.csv", "mode": "w"},
            ]
        )
    )

    assert result is None


def test_actor_accepts_fragment_append_without_streamer(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()
    result = ray.get(actor.append_fragment.remote({"job_uid": "feedcafe", "ray_task_id": "t-1"}))
    assert result is None


def test_actor_flush_to_glaas_is_true_without_streamer(ray_runtime) -> None:
    actor = RoarLogCollectorActor.remote()
    assert ray.get(actor.flush_to_glaas.remote()) is True
