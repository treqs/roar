"""Integration coverage for `roar workflow generate`."""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from tests.conftest import _run_roar_cmd

pytestmark = pytest.mark.integration


def _write_script(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _status_session_hash(roar_cli) -> str:
    result = roar_cli("status")
    assert result.returncode == 0, result.stderr or result.stdout
    match = re.search(r"DAG hash:\s+([0-9a-f]{64})", result.stdout)
    assert match is not None, f"Missing DAG hash in status output: {result.stdout}"
    return match.group(1)


def test_roar_workflow_generate_writes_default_treqs_workflow_for_active_session(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
) -> None:
    _write_script(
        temp_git_repo / "bootstrap.py",
        """
        from pathlib import Path

        Path("bootstrap.txt").write_text("ready\\n", encoding="utf-8")
        print("bootstrap complete")
        """,
    )
    _write_script(
        temp_git_repo / "train.py",
        """
        from pathlib import Path

        assert Path("bootstrap.txt").exists()
        Path("model.txt").write_text("trained\\n", encoding="utf-8")
        print("train complete")
        """,
    )
    git_commit("add pipeline scripts")

    roar_cli("env", "set", "DATA_DIR", "./data")

    result = roar_cli("build", "-n", "bootstrap", sys.executable, "bootstrap.py")
    assert result.returncode == 0, result.stderr or result.stdout
    git_commit("after bootstrap")

    result = roar_cli("run", "-n", "train", sys.executable, "train.py")
    assert result.returncode == 0, result.stderr or result.stdout
    git_commit("after train")

    result = roar_cli("workflow", "generate", "--name", "derived-pipeline")
    assert result.returncode == 0, result.stderr or result.stdout
    assert "Generated TReqs workflow: .treqs/workflows/derived-pipeline.yaml" in result.stdout
    assert "Tasks: 2" in result.stdout
    assert "@B1" in result.stdout
    assert "@2" in result.stdout

    workflow_path = temp_git_repo / ".treqs" / "workflows" / "derived-pipeline.yaml"
    assert workflow_path.exists()

    payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert list(payload.keys()) == ["name", "bootstrap", "train"]
    assert payload["name"] == "derived-pipeline"
    assert payload["bootstrap"].strip().endswith(f"{sys.executable} bootstrap.py")
    assert payload["train"].strip().endswith(f"{sys.executable} train.py")
    assert "export DATA_DIR=./data" in payload["bootstrap"]
    assert "export DATA_DIR=./data" in payload["train"]


def test_roar_workflow_generate_can_resolve_session_by_dag_hash_and_preserve_shared_subdir_cwd(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
) -> None:
    pipeline_dir = temp_git_repo / "pipeline"
    pipeline_dir.mkdir()
    _write_script(
        pipeline_dir / "prepare.py",
        """
        from pathlib import Path

        Path("prepared.txt").write_text("prepared\\n", encoding="utf-8")
        print("prepare complete")
        """,
    )
    _write_script(
        pipeline_dir / "train.py",
        """
        from pathlib import Path

        assert Path("prepared.txt").exists()
        Path("result.txt").write_text("done\\n", encoding="utf-8")
        print("train complete")
        """,
    )
    git_commit("add subdir pipeline")

    result = _run_roar_cmd(
        "run",
        "-n",
        "prepare",
        sys.executable,
        "prepare.py",
        cwd=pipeline_dir,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    git_commit("after prepare")

    result = _run_roar_cmd(
        "run",
        "-n",
        "train",
        sys.executable,
        "train.py",
        cwd=pipeline_dir,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    git_commit("after train")

    session_hash = _status_session_hash(roar_cli)

    result = roar_cli(
        "workflow",
        "generate",
        ".treqs/workflows/subdir-pipeline.yaml",
        "--session",
        session_hash[:12],
        "--name",
        "subdir-pipeline",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "Generated TReqs workflow: .treqs/workflows/subdir-pipeline.yaml" in result.stdout
    assert f"Source DAG: {session_hash}" in result.stdout
    assert "Working directory: pipeline" in result.stdout

    workflow_path = temp_git_repo / ".treqs" / "workflows" / "subdir-pipeline.yaml"
    payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert list(payload.keys()) == ["name", "working_directory", "prepare", "train"]
    assert payload["name"] == "subdir-pipeline"
    assert payload["working_directory"] == "pipeline"
    assert "cd pipeline" not in payload["prepare"]
    assert "cd pipeline" not in payload["train"]
    assert payload["prepare"].strip() == f"{sys.executable} prepare.py"
    assert payload["train"].strip() == f"{sys.executable} train.py"
