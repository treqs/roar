"""Probe a target Python interpreter's CPython ABI tag."""

from __future__ import annotations

import os
import subprocess

_PROBE_TIMEOUT_SECONDS = 5
_PROBE_SCRIPT = "import sys; print(sys.implementation.cache_tag)"


def probe_python_abi(executable: str) -> str | None:
    """Return the running ABI tag (e.g. ``cp312``) of ``executable``, or ``None``.

    Returns ``None`` if the target doesn't look like Python (we only probe
    invocations whose argv[0] basename starts with ``python``), the probe
    fails, or the interpreter is something exotic that doesn't expose
    ``sys.implementation.cache_tag``. Callers should treat ``None`` as
    "don't lazy-install for this target" — the sitecustomize gate handles
    whatever the traced process turns out to be.
    """
    if not executable:
        return None
    if not os.path.basename(executable).startswith("python"):
        return None
    try:
        result = subprocess.run(
            [executable, "-c", _PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    tag = result.stdout.strip()
    return tag or None
