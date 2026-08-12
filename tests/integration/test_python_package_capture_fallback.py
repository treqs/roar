from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _roar(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "command",
    [
        ["env", "PYTHONPATH=.", sys.executable, "-c", "import click"],
        ["env", "-u", "PYTHONPATH", sys.executable, "-c", "import click"],
        [sys.executable, "-E", "-c", "import click"],
        ["sh", "-c", f"PYTHONPATH=. {sys.executable} -c 'import click'"],
    ],
    ids=["env-replace", "env-unset", "python-ignore-env", "shell-replace"],
)
def test_native_trace_recovers_pin_when_sitecustomize_is_suppressed(
    tmp_path: Path, command: list[str]
) -> None:
    assert _roar(tmp_path, "init", "-n").returncode == 0
    assert _roar(tmp_path, "tracer", "use", "preload").returncode == 0

    run = _roar(tmp_path, "run", *command)
    assert run.returncode == 0, run.stderr

    shown = _roar(tmp_path, "show", "@1", "--all")
    assert shown.returncode == 0, shown.stderr
    assert "click==" in shown.stdout


def test_clean_environment_is_marked_incomplete_when_no_trace_survives(tmp_path: Path) -> None:
    assert _roar(tmp_path, "init", "-n").returncode == 0
    assert _roar(tmp_path, "tracer", "use", "preload").returncode == 0

    # env -i strips both PYTHONPATH and LD_PRELOAD from the Python grandchild.
    run = _roar(
        tmp_path,
        "run",
        "env",
        "-i",
        f"PATH={os.environ.get('PATH', '/usr/bin')}",
        sys.executable,
        "-c",
        "import click",
    )
    assert run.returncode == 0, run.stderr

    # The job is retained, but its explicit capture marker prevents the
    # reproducibility checklist from treating an empty freeze as trustworthy.
    import sqlite3

    connection = sqlite3.connect(tmp_path / ".roar" / "roar.db")
    row = connection.execute("SELECT metadata FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    connection.close()
    assert row is not None
    assert '"python_capture": "missing"' in row[0]
