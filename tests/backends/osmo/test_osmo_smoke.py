from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from .conftest import (
    HOST_PROJECTS_DIR,
    allow_git_safe_directory,
    container_repo_path,
    restore_host_path_ownership,
    roar_exec,
)

pytestmark = [pytest.mark.e2e, pytest.mark.osmo_e2e]
OSMO_SMOKE_TASK_IMAGE = "public.ecr.aws/docker/library/python:3.11-slim"


def _run_host(args: list[str], *, cwd: Path) -> None:
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "host command failed:\n"
            f"command: {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _prepare_product_project(
    project_dir: Path,
    *,
    runtime_install_requirement: str,
    task_image: str,
) -> None:
    shutil.rmtree(project_dir, ignore_errors=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    workflow_path = project_dir / "workflow.yaml"
    task_path = project_dir / "task.py"
    task_contents = """
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    output_path = Path(sys.argv[1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    message = "ROAR_OSMO_BASIC_OK"
    print(message)
    output_path.write_text(f"{message}\\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""".strip()

    workflow_contents = (
        """
workflow:
  name: {{ workflow_name }}
  resources:
    default:
      cpu: 1
      memory: 1Gi
      storage: 1Gi
  tasks:
    - name: basic
      image: {{ task_image }}
      command: ["python3"]
      args: ["/workspace/task.py", "{{output}}/result.txt"]
      files:
        - path: /workspace/task.py
          contents: |
""".strip("\n")
        + "\n"
        + textwrap.indent(task_contents, " " * 12)
        + """
      outputs:
        - dataset:
            name: {{ output_dataset }}
            path: result.txt

default-values:
  workflow_name: roar-osmo-basic
  output_dataset: roar-osmo-basic-output
  task_image: """
        + json.dumps(task_image)
        + "\n"
    )
    workflow_path.write_text(workflow_contents, encoding="utf-8")
    task_path.write_text(task_contents + "\n", encoding="utf-8")

    _run_host(["git", "init"], cwd=project_dir)
    _run_host(["git", "config", "user.email", "test@example.com"], cwd=project_dir)
    _run_host(["git", "config", "user.name", "Test User"], cwd=project_dir)

    container_project_dir = container_repo_path(project_dir)
    allow_git_safe_directory(container_project_dir)
    roar_exec(["init", "-y"], cwd=str(container_project_dir), timeout=5 * 60)
    for key, value in (
        ("osmo.wait_for_completion", "true"),
        ("osmo.download_declared_outputs", "true"),
        ("osmo.ingest_lineage_bundles", "true"),
        ("osmo.poll_interval_seconds", "2.0"),
        ("osmo.query_timeout_seconds", "900"),
        ("osmo.runtime_install_requirement", runtime_install_requirement),
    ):
        roar_exec(
            ["config", "set", key, value],
            cwd=str(container_project_dir),
            timeout=5 * 60,
        )

    _run_host(["git", "add", "-A"], cwd=project_dir)
    _run_host(["git", "commit", "-m", "Initialize OSMO product project"], cwd=project_dir)


def _host_visible_path(path: Path, *, project_dir: Path) -> Path:
    container_project_dir = container_repo_path(project_dir)
    if path.is_relative_to(container_project_dir):
        return project_dir / path.relative_to(container_project_dir)
    return path


def test_osmo_basic_workflow_submit_and_complete(
    osmo_harness: dict[str, str],
    osmo_runtime_wheel: dict[str, str],
) -> None:
    del osmo_harness
    workflow_name = f"roar-osmo-basic-{uuid.uuid4().hex[:8]}"
    output_dataset = f"{workflow_name}-output"
    project_dir = HOST_PROJECTS_DIR / workflow_name
    _prepare_product_project(
        project_dir,
        runtime_install_requirement=osmo_runtime_wheel["cluster_url"],
        task_image=OSMO_SMOKE_TASK_IMAGE,
    )

    submit = roar_exec(
        [
            "run",
            "osmo",
            "workflow",
            "submit",
            "workflow.yaml",
            "--pool",
            "default",
            "--set-string",
            f"workflow_name={workflow_name}",
            "--set-string",
            f"output_dataset={output_dataset}",
        ],
        cwd=str(container_repo_path(project_dir)),
        timeout=15 * 60,
    )
    restore_host_path_ownership(container_repo_path(project_dir))

    assert workflow_name in submit.stdout

    db_path = project_dir / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT id, job_uid, execution_backend, execution_role, exit_code
            FROM jobs
            WHERE execution_role = 'submit'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        child_jobs = conn.execute(
            """
            SELECT id, parent_job_uid, execution_backend, execution_role, command
            FROM jobs
            WHERE job_type = 'osmo_task'
            ORDER BY id ASC
            """
        ).fetchall()
        assert job is not None
        output_rows = conn.execute(
            """
            SELECT path
            FROM job_outputs
            WHERE job_id = ?
            ORDER BY path
            """,
            (int(job["id"]),),
        ).fetchall()
    finally:
        conn.close()

    assert job["execution_backend"] == "osmo"
    assert job["execution_role"] == "submit"
    assert job["exit_code"] == 0
    assert len(child_jobs) >= 1
    assert child_jobs[0]["parent_job_uid"] == job["job_uid"]
    assert child_jobs[0]["execution_backend"] == "osmo"
    assert child_jobs[0]["execution_role"] == "task"

    output_paths = [
        _host_visible_path(Path(str(row["path"])), project_dir=project_dir) for row in output_rows
    ]
    receipt_path = next(path for path in output_paths if "submissions" in str(path))
    query_path = next(path for path in output_paths if path.name == "query-COMPLETED.json")
    downloaded_result = next(path for path in output_paths if path.name == "result.txt")
    downloaded_bundle = next(path for path in output_paths if path.name == "roar-fragments.json")

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["osmo_submit"]["workflow_status"] == "COMPLETED"
    assert payload["osmo_submit"]["lineage_reconstitution"]["fragments_processed"] >= 1

    query_payload = json.loads(query_path.read_text(encoding="utf-8"))
    assert query_payload["status"] == "COMPLETED"

    assert downloaded_result.read_text(encoding="utf-8").strip() == "ROAR_OSMO_BASIC_OK"
    bundle_payload = json.loads(downloaded_bundle.read_text(encoding="utf-8"))
    assert len(bundle_payload.get("fragments", [])) >= 1
