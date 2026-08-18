"""Tests that a run records the command the user asked for.

The root process's argv used to be read back from /proc, which reports what the
kernel ran rather than what was requested. A `#!/usr/bin/env python3` script
therefore recorded as `/usr/bin/env python3 ./train.sh`, and a process that
exited before the read recorded nothing at all -- so the same run could be
recorded two different ways depending on machine load.

roar launches the workload, so its argv is known exactly. Descendants have no
such source and still come from /proc.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# On Apple Silicon the system launchers are arm64e platform binaries, and dyld
# refuses to insert the arm64 preload dylib into them:
#   incompatible architecture (have 'arm64', need 'arm64e')
_MACOS_PROTECTED_BINARY = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS protected system binaries reject preload injection",
)


def _roar(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _latest_command(cwd: Path) -> list[str]:
    connection = sqlite3.connect(cwd / ".roar" / "roar.db")
    row = connection.execute("SELECT metadata FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    connection.close()
    assert row is not None
    return (json.loads(row[0]).get("runtime") or {}).get("command")


@pytest.fixture
def initialised(tmp_path: Path) -> Path:
    assert _roar(tmp_path, "init", "-n").returncode == 0
    assert _roar(tmp_path, "tracer", "use", "preload").returncode == 0
    return tmp_path


def test_a_shebang_script_records_the_script_not_its_interpreter(initialised: Path) -> None:
    """The kernel rewrites cmdline to include the shebang interpreter, so /proc
    reports `<python> ./train.sh` for a run of `./train.sh`.

    The interpreter is named directly rather than via `/usr/bin/env` so this
    keeps running on macOS, where the system launchers are protected.
    """
    script = initialised / "train.sh"
    script.write_text(f"#!{sys.executable}\nprint('hi')\n")
    script.chmod(0o755)

    run = _roar(initialised, "run", "./train.sh")

    assert run.returncode == 0
    assert _latest_command(initialised) == ["./train.sh"]


@_MACOS_PROTECTED_BINARY
def test_a_wrapper_command_is_recorded_as_given(initialised: Path) -> None:
    run = _roar(initialised, "run", "env", "-u", "PYTHONPATH", sys.executable, "-c", "pass")

    assert run.returncode == 0
    assert _latest_command(initialised) == [
        "env",
        "-u",
        "PYTHONPATH",
        sys.executable,
        "-c",
        "pass",
    ]


@_MACOS_PROTECTED_BINARY
def test_a_short_lived_command_still_records_its_argv(initialised: Path) -> None:
    """A process that exits immediately may be a zombie by the time /proc is
    read, which is where the recorded argv used to vary with machine load."""
    for _ in range(5):
        run = _roar(initialised, "run", "/usr/bin/true")

        assert run.returncode == 0
        assert _latest_command(initialised) == ["/usr/bin/true"]
