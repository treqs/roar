"""Background telemetry uploader.

This is the only telemetry module that performs network imports or requests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from ._io import file_lock, read_json_object
from .config import resolve_config
from .paths import TelemetryPaths, resolve_paths
from .queue import QueuedEvent, cleanup_expired, delete_event, iter_queued_events

CONNECT_TIMEOUT_SECONDS = 2
TOTAL_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class UploadSummary:
    attempted: int
    accepted: int
    retained: int
    reason: str | None = None


def drain_queue(
    *,
    start_dir: str | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
) -> UploadSummary:
    """Drain queued telemetry events, deleting files only after HTTP 204."""

    resolved_paths = paths or resolve_paths(environ)
    effective = resolve_config(start_dir=start_dir, environ=environ, paths=resolved_paths)
    if not effective.stats_enabled:
        return UploadSummary(attempted=0, accepted=0, retained=0, reason="disabled")
    if not effective.endpoint:
        return UploadSummary(attempted=0, accepted=0, retained=0, reason="empty_endpoint")

    delivered: list[QueuedEvent] = []
    retained = 0
    with file_lock(resolved_paths.lock_file):
        cleanup_expired(paths=resolved_paths, locked=True)
        events = iter_queued_events(resolved_paths)

    for event in events:
        if _post_payload(effective.endpoint, event.payload):
            delivered.append(event)
        else:
            retained += 1

    accepted = 0
    if delivered:
        with file_lock(resolved_paths.lock_file):
            for event in delivered:
                if read_json_object(event.path) == event.payload:
                    delete_event(event.path)
                    accepted += 1
                else:
                    retained += 1
    return UploadSummary(attempted=len(events), accepted=accepted, retained=retained)


def main() -> int:
    drain_queue()
    # Piggyback the "newer roar available?" pypi check on this already-detached
    # background process so the foreground command never makes a network call.
    try:
        from ..version_check import refresh_pypi_version_cache

        refresh_pypi_version_cache()
    except Exception:  # pragma: no cover - opportunistic, must never break
        pass
    return 0


def _post_payload(endpoint: str, payload: Mapping[str, object]) -> bool:
    body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "roar-telemetry/1",
        },
        method="POST",
    )
    try:
        # urllib exposes a single socket timeout. Use the total budget here; the
        # connect timeout target is documented by CONNECT_TIMEOUT_SECONDS for a
        # future transport that can split connect/read deadlines precisely.
        with urllib.request.urlopen(request, timeout=TOTAL_TIMEOUT_SECONDS) as response:
            if hasattr(response, "read"):
                response.read()
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            return int(status or 0) == 204
    except urllib.error.HTTPError as exc:
        return int(getattr(exc, "code", 0) or 0) == 204
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
