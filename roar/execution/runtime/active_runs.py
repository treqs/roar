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


def write_marker(roar_dir: Path, *, pid: int, command: list[str], job_type: str | None) -> None:
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


def format_elapsed(seconds: float) -> str:
    """Render a small elapsed-time hint, e.g. ``2m14s`` or ``43s``."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs}s" if minutes else f"{secs}s"


def in_flight_run_warnings(roar_dir: Path | None) -> list[str]:
    """Warning lines for `roar run`/`roar build` processes still active on this host.

    Shared by any surface that wants to name this risk — `roar register`'s
    defaulted-active-session prompt and `roar status`'s summary both call this
    rather than duplicating the marker lookup + formatting.

    Best-effort: a missing/unreadable marker dir means "nothing detected", not
    an error — this must never block or fail the caller.
    """
    if roar_dir is None:
        return []
    try:
        markers = list_active_runs(roar_dir)
    except Exception:
        return []

    now = time.time()
    own_pid = os.getpid()
    lines: list[str] = []
    for marker in markers:
        pid = marker.get("pid")
        if pid == own_pid:
            continue
        job_type = marker.get("job_type") or "run"
        started_at = marker.get("started_at")
        elapsed = format_elapsed(now - started_at) if isinstance(started_at, (int, float)) else "?"
        command = marker.get("command")
        command_preview = " ".join(command) if isinstance(command, list) and command else None
        detail = f"pid {pid}, started {elapsed} ago"
        if command_preview:
            detail += f": `{command_preview}`"
        lines.append(
            f"Warning: a roar {job_type} ({detail}) appears to still be in progress — "
            "the active session may be incomplete."
        )
    return lines
