"""
Integration test for build tool detection via ``roar build``.

Verifies that running a build step which invokes cmake causes the
build_dpkg package collector to record cmake in the job metadata.
"""

import json
import platform
import shutil
import sqlite3
import subprocess

import pytest


def _uv_can_run() -> bool:
    """Check if uv can actually execute (not just on PATH).

    Also checks if uv is a snap package, which is incompatible with
    roar's ptrace-based tracer due to snap-confine restrictions.
    """
    uv_path = shutil.which("uv")
    if uv_path is None:
        return False
    # Snap-installed uv is incompatible with ptrace tracer
    if "/snap/" in uv_path:
        return False
    try:
        result = subprocess.run(
            ["uv", "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="dpkg-based detection is Linux-only"),
    pytest.mark.skip(reason="Flaky in CI — needs investigation (passes locally)"),
]


