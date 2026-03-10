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
        self._streamer: GlaasFragmentStreamer | None = None
        if session_id and token and glaas_url:
            self._streamer = GlaasFragmentStreamer(
                session_id=session_id,
                token=token,
                glaas_url=glaas_url,
            )

    def ping(self) -> bool:
        return True

    def append_batch(self, _events: list[dict]) -> None:
        # Deprecated compatibility shim for legacy worker hooks.
        return None

    def append_fragment(self, fragment: dict) -> None:
        if self._streamer is not None:
            self._streamer.append_fragment(fragment)

    def flush_to_glaas(self) -> bool:
        if self._streamer is None:
            return True
        return self._streamer.flush()
