"""Durable pending-publication markers (W1 / batch2 lineage-export durability).

`roar register` / `roar put` opens a GLaaS registration session, stages
jobs/artifacts, then finalizes — a multi-step network exchange. If the host dies
mid-exchange, the server is left holding a half-staged, unfinalized session and
the only pointer to it (`registration_session_id`) lived in memory, so `treqs
jobs republish-lineage` reports "No lineage package has been uploaded" and a
successful *paid* GPU run is stranded with no recovery path.

The lineage facts themselves are already durable in ``roar.db``. What was missing
is a breadcrumb that a publish was STARTED but never confirmed. This module writes
a marker to ``<roar_dir>/pending-publications/<session_hash>.json`` before staging
and removes it on success, so a crash leaves a discoverable on-disk record.
Recovery is then the already-supported ``roar register <session_hash>`` (it
re-collects from ``roar.db`` and re-publishes without re-running the workload).

Every function is best-effort: it swallows all errors and never raises, so wiring
it into the publish path can never break a publish. Writes are atomic (temp file
+ ``os.replace``) so a crash mid-write can't leave a torn marker.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

_PENDING_DIRNAME = "pending-publications"


def _pending_dir(roar_dir: str | os.PathLike[str]) -> Path:
    return Path(roar_dir) / _PENDING_DIRNAME


def _marker_path(roar_dir: str | os.PathLike[str], session_hash: str) -> Path:
    # Keep the filename filesystem-safe even if a hash ever carries odd chars.
    safe = "".join(c for c in session_hash if c.isalnum() or c in ("-", "_")) or "unknown"
    return _pending_dir(roar_dir) / f"{safe}.json"


def write_pending(
    roar_dir: str | os.PathLike[str],
    *,
    session_hash: str,
    registration_session_id: str | None,
    mode: str | None = None,
    target: str | None = None,
    now: float | None = None,
) -> None:
    """Record that a publish of ``session_hash`` has started. Best-effort/no-raise.

    ``registration_session_id`` is the orphaned server session (useful for
    debugging and a future resume path); recovery today only needs
    ``session_hash``.
    """
    try:
        directory = _pending_dir(roar_dir)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_hash": session_hash,
            "registration_session_id": registration_session_id,
            "mode": mode,
            "target": target,
            "started_epoch": time.time() if now is None else now,
        }
        path = _marker_path(roar_dir, session_hash)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True))
        os.replace(tmp, path)  # atomic within the same directory
    except Exception:
        # Durability is a safety net; never let it interfere with the publish.
        pass


def clear_pending(roar_dir: str | os.PathLike[str], session_hash: str) -> None:
    """Remove the marker for ``session_hash`` once the publish has succeeded.
    Best-effort/no-raise."""
    with contextlib.suppress(Exception):
        _marker_path(roar_dir, session_hash).unlink(missing_ok=True)


def list_pending(roar_dir: str | os.PathLike[str]) -> list[dict]:
    """Return the recorded pending-publication markers (empty on any failure).

    Each is a publish that started but never confirmed — recoverable with
    ``roar register <session_hash>``. Unreadable/partial markers are skipped.
    """
    markers: list[dict] = []
    try:
        directory = _pending_dir(roar_dir)
        if not directory.is_dir():
            return []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("session_hash"):
                markers.append(data)
    except Exception:
        return markers
    return markers


__all__ = ["clear_pending", "list_pending", "write_pending"]
