"""Product-path coverage for local `roar reproduce --run` flows."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _query_rows(repo_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    db_path = repo_path / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def test_reproduce_run_reuses_matching_local_repo_and_recreates_artifact(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    local_remote = f"file://{temp_git_repo}"
    subprocess.run(
        ["git", "remote", "add", "origin", local_remote],
        cwd=temp_git_repo,
        capture_output=True,
        check=True,
    )

    script = temp_git_repo / "generate_model.py"
    script.write_text(
        "from pathlib import Path\nPath('model.bin').write_bytes(b'local-reproduction-payload')\n"
    )
    git_commit("Add reproduction fixture")

    run_result = roar_cli("run", python_exe, "generate_model.py")
    assert run_result.returncode == 0

    original_output = temp_git_repo / "model.bin"
    original_bytes = original_output.read_bytes()
    assert original_bytes == b"local-reproduction-payload"

    lineage_result = roar_cli("lineage", "model.bin")
    lineage = json.loads(lineage_result.stdout)
    artifact_hash = lineage["artifact"]["hash"]
    assert len(artifact_hash) >= 12

    active_sessions = _query_rows(
        temp_git_repo,
        "SELECT git_repo, git_commit_start FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1",
    )
    assert active_sessions
    assert active_sessions[0]["git_repo"] == local_remote
    assert active_sessions[0]["git_commit_start"] is not None

    git_commit("Commit generated artifact")
    original_output.unlink()
    assert not original_output.exists()

    reproduce_result = roar_cli("reproduce", artifact_hash[:12], "--run", "-y")

    assert reproduce_result.returncode == 0
    assert "Current repository matches artifact remote, using existing environment" in (
        reproduce_result.stdout
    )
    assert "Reproduction Complete" in reproduce_result.stdout
    assert "Steps run: 1/1" in reproduce_result.stdout
    assert original_output.read_bytes() == original_bytes
