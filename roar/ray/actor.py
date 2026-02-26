from __future__ import annotations

import ray


@ray.remote(num_cpus=0, max_concurrency=500)
class RoarLogCollectorActor:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._fragments: list[dict] = []

    def append_batch(self, events: list[dict]) -> None:
        self._events.extend(events)

    def get_all(self) -> list[dict]:
        return list(self._events)

    def append_fragment(self, fragment: dict) -> None:
        self._fragments.append(fragment)

    def get_all_fragments(self) -> list[dict]:
        return list(self._fragments)
