"""Atomic local telemetry event queue."""

from __future__ import annotations

import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._io import atomic_write_json, file_lock, read_json_object
from .paths import TelemetryPaths, resolve_paths

DEFAULT_RETENTION_DAYS = 7
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class QueueResult:
    event_id: str
    path: Path | None
    enqueued: bool
    reason: str | None = None


@dataclass(frozen=True)
class QueuedEvent:
    path: Path
    payload: dict[str, Any]


def enqueue_payload(
    payload: dict[str, Any],
    *,
    paths: TelemetryPaths | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> QueueResult:
    """Atomically enqueue a payload, deduping by event_id."""

    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("telemetry payload is missing event_id")
    if _EVENT_ID_RE.fullmatch(event_id) is None:
        raise ValueError("telemetry payload event_id contains unsupported characters")

    resolved_paths = paths or resolve_paths()
    queue_path = resolved_paths.queue_dir / f"{event_id}.json"
    with file_lock(resolved_paths.lock_file):
        cleanup_expired(paths=resolved_paths, retention_days=retention_days, locked=True)
        if queue_path.exists():
            return QueueResult(
                event_id=event_id,
                path=queue_path,
                enqueued=False,
                reason="duplicate_event_id",
            )
        atomic_write_json(queue_path, payload)
        return QueueResult(event_id=event_id, path=queue_path, enqueued=True)


def queued_event_count(paths: TelemetryPaths | None = None) -> int:
    resolved_paths = paths or resolve_paths()
    if not resolved_paths.queue_dir.exists():
        return 0
    return len(list(_queue_files(resolved_paths.queue_dir)))


def iter_queued_events(paths: TelemetryPaths | None = None) -> tuple[QueuedEvent, ...]:
    resolved_paths = paths or resolve_paths()
    events: list[QueuedEvent] = []
    for path in _queue_files(resolved_paths.queue_dir):
        payload = read_json_object(path)
        if payload:
            events.append(QueuedEvent(path=path, payload=payload))
    return tuple(events)


def cleanup_expired(
    *,
    paths: TelemetryPaths | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    locked: bool = False,
) -> int:
    resolved_paths = paths or resolve_paths()
    if locked:
        return _cleanup_expired_unlocked(resolved_paths, retention_days)
    with file_lock(resolved_paths.lock_file):
        return _cleanup_expired_unlocked(resolved_paths, retention_days)


def delete_event(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _cleanup_expired_unlocked(paths: TelemetryPaths, retention_days: int) -> int:
    cutoff = time.time() - max(retention_days, 0) * 24 * 60 * 60
    removed = 0
    for path in _queue_files(paths.queue_dir):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _queue_files(queue_dir: Path) -> tuple[Path, ...]:
    if not queue_dir.exists():
        return ()
    return tuple(
        sorted(
            (
                path
                for path in queue_dir.glob("*.json")
                if path.is_file() and not path.name.startswith(".")
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
    )
