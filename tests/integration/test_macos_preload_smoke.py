"""macOS-specific smoke test for preload tracer stability."""

from __future__ import annotations

import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only preload smoke test")
def test_roar_run_python_smoke_on_macos(temp_git_repo, roar_cli, git_commit, python_exe):
    """`roar run` should trace a simple Python file-I/O script without crashing."""
    script = temp_git_repo / "smoke.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('smoke.txt').write_text('ok')\n"
        "_ = Path('smoke.txt').read_text()\n"
    )
    git_commit("add macOS preload smoke script")

    result = roar_cli("run", python_exe, "smoke.py")
    assert result.returncode == 0
    assert (temp_git_repo / "smoke.txt").exists()
