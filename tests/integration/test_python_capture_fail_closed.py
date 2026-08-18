from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_MACOS_PROTECTED_BINARY = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS protected system binaries reject preload before the workload starts",
)

# `true` is /bin/true on most Linux distributions but only /usr/bin/true on
# macOS, where a hardcoded /bin/true exits 127 and looks like a roar failure.
# On macOS it is SIP-protected either way, so its test carries the skip above.
_TRUE_BINARY = shutil.which("true") or "/usr/bin/true"


def _roar(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _latest_metadata(cwd: Path) -> dict:
    connection = sqlite3.connect(cwd / ".roar" / "roar.db")
    row = connection.execute("SELECT metadata FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    connection.close()
    assert row is not None
    return json.loads(row[0])


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            ["env", "PYTHONPATH=.", sys.executable, "-c", "import click"],
            marks=_MACOS_PROTECTED_BINARY,
            id="env-replace",
        ),
        pytest.param(
            ["env", "-u", "PYTHONPATH", sys.executable, "-c", "import click"],
            marks=_MACOS_PROTECTED_BINARY,
            id="env-unset",
        ),
        pytest.param([sys.executable, "-E", "-c", "import click"], id="python-E"),
        pytest.param([sys.executable, "-I", "-c", "import click"], id="python-I"),
        pytest.param([sys.executable, "-S", "-c", "pass"], id="python-S"),
        pytest.param(
            ["sh", "-c", f"PYTHONPATH=. {sys.executable} -c 'import click'"],
            marks=_MACOS_PROTECTED_BINARY,
            id="shell-replace",
        ),
    ],
)
def test_suppressed_injection_warns_and_records_failed_capture(
    tmp_path: Path, command: list[str]
) -> None:
    assert _roar(tmp_path, "init", "-n").returncode == 0
    assert _roar(tmp_path, "tracer", "use", "preload").returncode == 0

    run = _roar(tmp_path, "run", *command)

    assert run.returncode == 0
    assert "Python package capture did not complete" in run.stderr
    assert _latest_metadata(tmp_path)["python_capture"] == "missing"


def test_successful_python_capture_has_no_warning(tmp_path: Path) -> None:
    assert _roar(tmp_path, "init", "-n").returncode == 0
    assert _roar(tmp_path, "tracer", "use", "preload").returncode == 0

    run = _roar(tmp_path, "run", sys.executable, "-c", "import click")

    assert run.returncode == 0
    assert "Python package capture did not complete" not in run.stderr
    assert _latest_metadata(tmp_path)["python_capture"] == "complete"


@_MACOS_PROTECTED_BINARY
def test_non_python_command_is_not_misreported(tmp_path: Path) -> None:
    assert _roar(tmp_path, "init", "-n").returncode == 0
    assert _roar(tmp_path, "tracer", "use", "preload").returncode == 0

    run = _roar(tmp_path, "run", _TRUE_BINARY)

    assert run.returncode == 0
    assert "Python package capture did not complete" not in run.stderr
    assert _latest_metadata(tmp_path)["python_capture"] == "not-applicable"
