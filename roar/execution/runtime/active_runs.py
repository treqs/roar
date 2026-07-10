"""
Active-run markers for concurrent-run detection.

`RunCoordinator.execute()` writes one of these markers before starting the
tracer and removes it when the run finishes (see `active_run_marker`), so a
separate `roar register` invocation against the same `.roar` directory can
warn when a `roar run`/`roar build` still appears to be in flight. Mirrors
the PID-liveness pattern already used for the S3 proxy daemon
(`execution/cluster/proxy.py`'s `proxy.json` + `os.kill(pid, 0)`), scoped to
this host rather than a shared/remote signal.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _markers_dir(roar_dir: Path) -> Path:
    return Path(roar_dir) / "active_runs"


def write_marker(
    roar_dir: Path, *, pid: int, command: list[str], job_type: str | None
) -> None:
    """Record that `pid` has a run/build in flight against `roar_dir`.

    Best-effort: a missing/unwritable `.roar` dir must never fail the run.
    """
    try:
        markers_dir = _markers_dir(roar_dir)
        markers_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "pid": pid,
            "started_at": time.time(),
            "command": command,
            "job_type": job_type,
        }
        (markers_dir / f"{pid}.json").write_text(json.dumps(info))
    except OSError:
        pass


def remove_marker(roar_dir: Path, *, pid: int) -> None:
    """Remove a marker written by `write_marker`. Best-effort; never raises."""
    with contextlib.suppress(OSError):
        (_markers_dir(roar_dir) / f"{pid}.json").unlink(missing_ok=True)


@contextlib.contextmanager
def active_run_marker(
    roar_dir: Path, *, pid: int, command: list[str], job_type: str | None
) -> Iterator[None]:
    """Write a marker for the duration of the enclosed block, cleaned up on exit."""
    write_marker(roar_dir, pid=pid, command=command, job_type=job_type)
    try:
        yield
    finally:
        remove_marker(roar_dir, pid=pid)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def list_active_runs(roar_dir: Path) -> list[dict[str, Any]]:
    """Return markers for runs that still appear to be alive on this host.

    Self-healing: markers for PIDs that are no longer alive (crash, `kill -9`,
    a run that finished without going through `active_run_marker`'s cleanup)
    are deleted as they're found rather than reported.
    """
    markers_dir = _markers_dir(roar_dir)
    if not markers_dir.is_dir():
        return []

    active: list[dict[str, Any]] = []
    for marker_path in markers_dir.glob("*.json"):
        try:
            info = json.loads(marker_path.read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            marker_path.unlink(missing_ok=True)
            continue

        if _is_alive(pid):
            active.append(info)
        else:
            marker_path.unlink(missing_ok=True)

    return active
