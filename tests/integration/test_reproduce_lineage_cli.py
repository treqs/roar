"""Integration coverage for explicit `roar reproduce --lineage` flows."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from .fake_glaas import FakeGlaasServer

pytestmark = pytest.mark.integration


def _status_lineage_hash(roar_cli) -> str:
    result = roar_cli("status")
    assert result.returncode == 0, result.stderr or result.stdout
    match = re.search(r"DAG hash:\s+([0-9a-f]{64})", result.stdout)
    assert match is not None, f"Missing DAG hash in status output: {result.stdout}"
    return match.group(1)


def test_reproduce_lineage_run_reuses_matching_local_repo_and_recreates_outputs(
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
        "from pathlib import Path\nPath('model.bin').write_bytes(b'lineage-reproduction-payload')\n"
    )
    git_commit("Add lineage reproduction fixture")

    run_result = roar_cli("run", python_exe, "generate_model.py")
    assert run_result.returncode == 0

    lineage_hash = _status_lineage_hash(roar_cli)
    original_output = temp_git_repo / "model.bin"
    original_bytes = original_output.read_bytes()
    assert original_bytes == b"lineage-reproduction-payload"

    git_commit("Commit generated artifact")
    original_output.unlink()
    assert not original_output.exists()

    reproduce_result = roar_cli("reproduce", lineage_hash, "--lineage", "--run", "-y")

    assert reproduce_result.returncode == 0
    assert f"Found lineage: {lineage_hash}" in reproduce_result.stdout
    assert "Current repository matches recorded remote, using existing environment" in (
        reproduce_result.stdout
    )
    assert "Reproduction Complete" in reproduce_result.stdout
    assert "Steps run: 1/1" in reproduce_result.stdout
    assert original_output.read_bytes() == original_bytes


def test_reproduce_lineage_preview_reads_remote_session_payload_and_can_write_output(
    temp_git_repo: Path,
    roar_cli,
    fake_glaas_server: FakeGlaasServer,
) -> None:
    session_hash = "c" * 64
    out_path = temp_git_repo / "session-reproduction.json"
    fake_glaas_server.set_session_reproduction(
        session_hash,
        {
            "sessionHash": session_hash,
            "gitRepo": "https://github.com/test/repro.git",
            "gitCommit": "deadbeef",
            "gitBranch": "main",
            "jobs": [
                {
                    "jobUid": "job-build",
                    "command": "python build_env.py",
                    "jobType": "build",
                    "stepNumber": 1,
                    "metadata": {"cwd": "pipeline"},
                    "inputs": [],
                    "outputs": [{"hash": "a" * 64, "path": "config.json"}],
                },
                {
                    "jobUid": "job-run",
                    "command": "python train.py",
                    "jobType": "run",
                    "stepNumber": 2,
                    "metadata": {"env_vars": {"DATA_DIR": "./data"}},
                    "inputs": [{"hash": "b" * 64, "path": "train.csv"}],
                    "outputs": [{"hash": "d" * 64, "path": "model.bin"}],
                },
            ],
        },
    )
    roar_cli("config", "set", "glaas.url", fake_glaas_server.base_url)

    preview = roar_cli(
        "reproduce",
        session_hash,
        "--lineage",
        "--out",
        str(out_path),
        check=False,
    )

    assert preview.returncode == 0
    assert f"Session reproduction response written to {out_path}" in preview.stdout
    assert f"Lineage: {session_hash}" in preview.stdout
    assert "Build Steps (1):" in preview.stdout
    assert "Run Steps (1):" in preview.stdout
    assert "--lineage" in preview.stdout
    written = json.loads(out_path.read_text())
    assert written["sessionHash"] == session_hash
    assert written["jobs"][0]["jobType"] == "build"
    assert written["jobs"][1]["jobType"] == "run"


@pytest.fixture
def fake_glaas_server():
    with FakeGlaasServer() as server:
        yield server
