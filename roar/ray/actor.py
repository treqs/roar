from __future__ import annotations

import ray

from roar.ray.glaas_fragment_streamer import GlaasFragmentStreamer


@ray.remote(num_cpus=0, max_concurrency=500)  # type: ignore[call-overload]
class RoarLogCollectorActor:
    def __init__(
        self,
        session_id: str | None = None,
        token: str | None = None,
        glaas_url: str | None = None,
    ) -> None:
        self._events: list[dict] = []
        self._fragments: list[dict] = []
        self._streamer: GlaasFragmentStreamer | None = None

        if session_id and token and glaas_url:
            self._streamer = GlaasFragmentStreamer(
                session_id=session_id,
                token=token,
                glaas_url=glaas_url,
            )

    def append_batch(self, events: list[dict]) -> None:
        self._events.extend(events)

    def get_all(self) -> list[dict]:
        return list(self._events)

    def append_fragment(self, fragment: dict) -> None:
        self._fragments.append(fragment)
        if self._streamer is not None:
            self._streamer.append_fragment(fragment)

    def get_all_fragments(self) -> list[dict]:
        return list(self._fragments)

    def flush_to_glaas(self) -> bool:
        if self._streamer is None:
            return True
        return self._streamer.flush()
