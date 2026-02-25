"""
roar Ray worker setup hook.

Installed via runtime_env.worker_process_setup_hook when ROAR_WRAP=1.
Patches builtins.open to capture per-task file I/O, writing each event
immediately to ROAR_LOG_DIR/<task_id>.jsonl on the shared volume.
"""
from __future__ import annotations

import builtins
import json
import os
import time

# Captured at module import time so the hook doesn't recursively call itself.
_real_open = builtins.open

_LOG_DIR: str = ""
_SKIP_PREFIXES: tuple[str, ...] = ()


def setup() -> None:
    """
    Called once by Ray when a new worker process starts.

    Sets up the file I/O tracking shim.  Writes are non-blocking:
    each open() call appends a JSON line to the shared log dir.
    """
    global _LOG_DIR, _SKIP_PREFIXES

    _LOG_DIR = os.environ.get("ROAR_LOG_DIR", "/shared/.roar-logs")
    os.makedirs(_LOG_DIR, exist_ok=True)

    # Paths we must never recurse into (the log dir itself, /proc, /sys …)
    _SKIP_PREFIXES = (
        _LOG_DIR,
        "/proc/",
        "/sys/",
        "/dev/",
    )

    builtins.open = _tracking_open


def _tracking_open(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """Replacement for builtins.open that logs file access with task context."""
    result = _real_open(*args, **kwargs)

    try:
        raw_path = args[0] if args else kwargs.get("file", "")
        if isinstance(raw_path, (str, bytes, os.PathLike)):
            path = os.path.abspath(os.fspath(raw_path))
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")

            # Skip our own log files and pseudo-filesystems.
            if not any(path.startswith(p) for p in _SKIP_PREFIXES):
                _log_access(path, str(mode))
    except Exception:  # noqa: BLE001
        pass  # Never let tracking errors break user code

    return result


def _log_access(path: str, mode: str) -> None:
    """Append one JSON line to the task-specific log file."""
    try:
        import ray  # noqa: PLC0415

        ctx = ray.get_runtime_context()
        task_id = ctx.get_task_id()
    except Exception:  # noqa: BLE001
        return  # Not inside a task context; skip

    if not task_id:
        return

    log_file = os.path.join(_LOG_DIR, f"{task_id}.jsonl")
    entry = json.dumps({"path": path, "mode": mode, "task_id": task_id, "ts": time.time()})
    # Use _real_open so we don't recurse through our own hook.
    with _real_open(log_file, "a") as fh:
        fh.write(entry + "\n")
