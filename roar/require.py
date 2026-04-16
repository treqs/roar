"""Guard module: abort if the current process is not running under ``roar run``.

Usage in user scripts::

    from roar import require   # exits if not under roar run

The import itself is the guard — no function call is needed.
Set ``ROAR_GUARD=0`` in the environment to bypass (e.g. in tests).
"""

from __future__ import annotations

import os
import sys


def _is_under_roar() -> bool:
    if os.environ.get("ROAR_GUARD") == "0":
        return True
    if _is_execution_backend_job_environment():
        return True
    return _check_process_tree()


def _is_execution_backend_job_environment() -> bool:
    """Return True when the current process is already inside a Roar job env."""
    try:
        from roar.execution.framework.registry import is_execution_backend_job_environment
    except Exception:
        return False

    try:
        return bool(is_execution_backend_job_environment(os.environ))
    except Exception:
        return False


def _check_process_tree() -> bool:
    """Walk ancestor PIDs looking for a ``roar`` process."""
    if sys.platform == "linux":
        return _walk_linux()
    if sys.platform == "darwin":
        return _walk_macos()
    # Unsupported platform — fail open so the user isn't blocked.
    return True


def _walk_linux() -> bool:
    pid = os.getpid()
    while pid > 1:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            # Format: pid (comm) state ppid ...
            # comm is in parens and may contain spaces; kernel truncates to 15 chars.
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
            if _is_roar_process(comm):
                return True
            rest = stat[stat.rindex(")") + 2 :].split()
            pid = int(rest[1])  # state=rest[0], ppid=rest[1]
        except (FileNotFoundError, ValueError, IndexError, PermissionError):
            break
    return False


def _walk_macos() -> bool:
    import subprocess

    pid = os.getpid()
    while pid > 1:
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                break
            line = result.stdout.strip()
            if not line:
                break
            parts = line.split(None, 1)
            if len(parts) < 2:
                break
            ppid_str, comm = parts
            name = os.path.basename(comm)
            if _is_roar_process(name):
                return True
            pid = int(ppid_str)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            break
    return False


def _is_roar_process(comm: str) -> bool:
    """Return True if *comm* looks like a roar CLI or tracer binary.

    On Linux ``/proc/*/stat`` truncates to 15 chars, so
    ``roar-tracer-preload`` → ``roar-tracer-pre``, etc.
    """
    return comm == "roar" or comm.startswith("roar-tracer")


# ---- module-level guard ----
if not _is_under_roar():
    print(
        "Error: This script requires roar provenance tracking.\n"
        "\n"
        "  Run it with:  roar run <your-command>\n"
        "\n"
        "To bypass this check (e.g. in tests), set ROAR_GUARD=0",
        file=sys.stderr,
    )
    sys.exit(1)
