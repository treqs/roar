from __future__ import annotations

import ray


@ray.remote(num_cpus=0, max_concurrency=500)
class RoarLogCollectorActor:
    def __init__(self) -> None:
        self._events: list[dict] = []

    def append_batch(self, events: list[dict]) -> None:
        self._events.extend(events)

    def get_all(self) -> list[dict]:
        return list(self._events)
