"""Product-path coverage for local `roar put` flows."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .fake_glaas import FakeGlaasServer

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_glaas_publish_server() -> FakeGlaasServer:
    with FakeGlaasServer() as server:
        yield server


def _configure_put_repo(repo: Path, roar_cli, fake_glaas_url: str) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    roar_cli("config", "set", "glaas.url", fake_glaas_url)
    roar_cli("config", "set", "glaas.web_url", fake_glaas_url)


def _create_repo_with_outputs(
    repo: Path,
    *,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    script = repo / "train.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('model.pt').write_bytes(b'fake model weights' * 10)\n"
        "Path('metrics.json').write_text('{\"accuracy\": 0.95}')\n"
    )
    git_commit("Add training script")

    run_result = roar_cli("run", python_exe, "train.py")
    assert run_result.returncode == 0

    git_commit("Add training outputs")


def _get_dag(roar_cli) -> dict[str, Any]:
    return json.loads(roar_cli("dag", "--json", "--show-artifacts").stdout)


def test_put_registers_lineage_with_fake_glaas_and_updates_local_dag(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    monkeypatch,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    _configure_put_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_repo_with_outputs(
        temp_git_repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        python_exe=python_exe,
    )
    monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "1")

    result = roar_cli("put", "model.pt", "s3://test-bucket/models", "-m", "publish model")

    assert result.returncode == 0
    assert "Published 1 file(s) to s3://test-bucket/models" in result.stdout
    assert "model.pt -> s3://test-bucket/models/model.pt" in result.stdout
    assert "Job created: step 2" in result.stdout
    assert f"View: {fake_glaas_publish_server.base_url}/dag/" in result.stdout

    dag = _get_dag(roar_cli)
    assert dag["total_steps"] == 2
    nodes_by_step = {node["step_number"]: node for node in dag["nodes"]}
    put_node = nodes_by_step[2]
    assert "roar put model.pt -m \"publish model\"" in put_node["command"]
    assert put_node["metrics"]["inputs"] >= 1
    assert put_node["metrics"]["outputs"] == 0
    assert 1 in put_node["dependencies"]

    assert fake_glaas_publish_server.health_checks >= 1
    assert len(fake_glaas_publish_server.session_registrations) == 1
    assert len(fake_glaas_publish_server.job_batches) == 1
    assert len(fake_glaas_publish_server.job_creates) == 1
    assert len(fake_glaas_publish_server.artifact_batches) >= 1
    assert fake_glaas_publish_server.input_links
    assert fake_glaas_publish_server.output_links

    batch_jobs = fake_glaas_publish_server.job_batches[0]["jobs"]
    assert any(job.get("job_type") == "run" for job in batch_jobs)
    assert fake_glaas_publish_server.job_creates[0]["job"]["job_type"] == "put"


def test_put_dry_run_does_not_create_local_or_remote_publish_jobs(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    monkeypatch,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    _configure_put_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_repo_with_outputs(
        temp_git_repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        python_exe=python_exe,
    )
    monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "1")

    dag_before = _get_dag(roar_cli)

    result = roar_cli(
        "put",
        "model.pt",
        "metrics.json",
        "s3://bucket/test",
        "-m",
        "test",
        "--dry-run",
    )

    assert result.returncode == 0
    assert "Dry run - would upload:" in result.stdout
    assert "model.pt" in result.stdout
    assert "metrics.json" in result.stdout
    assert "Total: 2 file(s)" in result.stdout

    dag_after = _get_dag(roar_cli)
    assert dag_after["total_steps"] == dag_before["total_steps"] == 1
    assert len(fake_glaas_publish_server.job_batches) == 0
    assert len(fake_glaas_publish_server.job_creates) == 0
    assert len(fake_glaas_publish_server.artifact_batches) == 0
    assert len(fake_glaas_publish_server.input_links) == 0
    assert len(fake_glaas_publish_server.output_links) == 0
