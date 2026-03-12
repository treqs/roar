from __future__ import annotations

import json


def _write_backend_probe(temp_git_repo, output_name: str) -> None:
    script = temp_git_repo / "backend_probe.py"
    script.write_text(
        """
import json
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps({"backend": os.environ.get("ROAR_EXECUTION_BACKEND")}),
    encoding="utf-8",
)
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_run_uses_local_execution_backend(temp_git_repo, roar_cli, git_commit, python_exe) -> None:
    _write_backend_probe(temp_git_repo, "run_backend.json")
    git_commit("add local backend run probe")

    result = roar_cli("run", python_exe, "backend_probe.py", "run_backend.json")

    assert result.returncode == 0
    payload = json.loads((temp_git_repo / "run_backend.json").read_text(encoding="utf-8"))
    assert payload == {"backend": "local"}


def test_build_uses_local_execution_backend(
    temp_git_repo, roar_cli, git_commit, python_exe
) -> None:
    _write_backend_probe(temp_git_repo, "build_backend.json")
    git_commit("add local backend build probe")

    result = roar_cli("build", python_exe, "backend_probe.py", "build_backend.json")

    assert result.returncode == 0
    payload = json.loads((temp_git_repo / "build_backend.json").read_text(encoding="utf-8"))
    assert payload == {"backend": "local"}
