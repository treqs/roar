from __future__ import annotations

import json
import sqlite3
import stat
import textwrap
from pathlib import Path


def _write_fake_osmo(temp_git_repo: Path) -> None:
    script = temp_git_repo / "osmo"
    script.write_text(
        textwrap.dedent(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["workflow", "submit"]:
    if len(args) >= 3:
        submit_path = Path(args[2])
        (Path.cwd() / ".submitted-osmo-command.json").write_text(
            json.dumps(args),
            encoding="utf-8",
        )
        (Path.cwd() / ".submitted-osmo-workflow.yaml").write_text(
            submit_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "name": "workflow-product",
                "overview": "https://osmo.example/workflows/workflow-product",
            }
        )
    )
    raise SystemExit(0)
if args[:2] == ["workflow", "query"] and len(args) >= 3:
    print(json.dumps({"name": args[2], "status": "COMPLETED"}))
    raise SystemExit(0)
if args[:2] == ["dataset", "download"] and len(args) >= 4:
    target = Path(args[3])
    target.mkdir(parents=True, exist_ok=True)
    dataset_ref = args[2]
    if dataset_ref.startswith("roar-lineage:"):
        payload = {
            "fragments": [
                {
                    "job_uid": "osmo-product-task",
                    "task_id": "basic-task",
                    "worker_id": "worker-1",
                    "node_id": "node-1",
                    "task_name": "basic",
                    "started_at": 1.0,
                    "ended_at": 2.0,
                    "exit_code": 0,
                    "backend": "osmo",
                    "reads": [
                        {
                            "path": "workflow.yaml",
                            "hash": "workflowhash",
                            "hash_algorithm": "blake3",
                            "size": 1,
                            "capture_method": "python",
                        }
                    ],
                    "writes": [
                        {
                            "path": "${ROAR_PROJECT_DIR}/outputs/worker-output.txt",
                            "hash": "workeroutputhash",
                            "hash_algorithm": "blake3",
                            "size": 17,
                            "capture_method": "python",
                        }
                    ],
                    "backend_metadata": {"execution_role": "task"},
                }
            ]
        }
        (target / "roar-fragments.json").write_text(json.dumps(payload), encoding="utf-8")
    else:
        (target / "result.txt").write_text("ROAR_OSMO_BASIC_OK\\n", encoding="utf-8")
    raise SystemExit(0)

print(f"unexpected args: {args!r}", file=sys.stderr)
raise SystemExit(2)
"""
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_roar_run_osmo_workflow_submit_records_receipt_and_waits(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
) -> None:
    _write_fake_osmo(temp_git_repo)
    (temp_git_repo / "task.py").write_text("print('hello')\n", encoding="utf-8")
    (temp_git_repo / "workflow.yaml").write_text(
        """
workflow:
  name: {{ workflow_name }}
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
      files:
        - localpath: ./task.py
          path: /workspace/task.py
      outputs:
        - dataset:
            name: {{ output_dataset }}
            path: result.txt
        - dataset:
            name: roar-lineage
            path: roar-fragments.json

default-values:
  workflow_name: roar-osmo-basic
  output_dataset: roar-osmo-basic-output
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        "[osmo]\n"
        "wait_for_completion = true\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n"
        "poll_interval_seconds = 0.01\n"
        "query_timeout_seconds = 2\n",
        encoding="utf-8",
    )
    git_commit("add fake osmo submit backend")

    result = roar_cli(
        "run",
        "./osmo",
        "workflow",
        "submit",
        "workflow.yaml",
        "--set-string",
        "workflow_name=workflow-product",
        "--set-string",
        "output_dataset=workflow-product-output",
    )

    assert result.returncode == 0
    assert "workflow-product" in result.stdout

    db_path = temp_git_repo / ".roar" / "roar.db"
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
        child_outputs = conn.execute(
            """
            SELECT jo.path
            FROM job_outputs jo
            JOIN jobs j ON j.id = jo.job_id
            WHERE j.job_type = 'osmo_task'
            ORDER BY jo.path
            """
        ).fetchall()
        assert job is not None
        output = conn.execute(
            """
            SELECT path
            FROM job_outputs
            WHERE job_id = ?
            ORDER BY path
            """,
            (int(job["id"]),),
        ).fetchall()
        inputs = conn.execute(
            """
            SELECT path
            FROM job_inputs
            WHERE job_id = ?
            ORDER BY path
            """,
            (int(job["id"]),),
        ).fetchall()
    finally:
        conn.close()

    assert int(job["id"]) > 0
    assert job["execution_backend"] == "osmo"
    assert job["execution_role"] == "submit"
    assert job["exit_code"] == 0
    assert len(child_jobs) == 1
    assert child_jobs[0]["parent_job_uid"] == job["job_uid"]
    assert child_jobs[0]["execution_backend"] == "osmo"
    assert child_jobs[0]["execution_role"] == "task"
    assert child_jobs[0]["command"] == "osmo_task:basic"
    assert [str(row["path"]) for row in child_outputs] == [
        str(temp_git_repo / "outputs" / "worker-output.txt")
    ]
    output_paths = [Path(str(row["path"])) for row in output]
    receipt_path = next(path for path in output_paths if "submissions" in str(path))
    query_path = next(path for path in output_paths if path.name == "query-COMPLETED.json")
    assert (
        receipt_path
        == temp_git_repo / ".roar" / "osmo" / "submissions" / "workflow-product-COMPLETED.json"
    )
    downloaded_result = next(path for path in output_paths if path.name == "result.txt")
    downloaded_bundle = next(path for path in output_paths if path.name == "roar-fragments.json")
    assert downloaded_result.exists()
    assert downloaded_result.read_text(encoding="utf-8").strip() == "ROAR_OSMO_BASIC_OK"
    assert downloaded_bundle.exists()
    assert query_path.exists()
    assert [str(row["path"]) for row in inputs] == [str(temp_git_repo / "workflow.yaml")]

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["osmo_submit"]["workflow_id"] == "workflow-product"
    assert payload["osmo_submit"]["workflow_status"] == "COMPLETED"
    assert (
        payload["osmo_submit"]["response"]["overview"]
        == "https://osmo.example/workflows/workflow-product"
    )


def test_roar_run_osmo_workflow_submit_transparently_prepares_install_wrapper(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
) -> None:
    _write_fake_osmo(temp_git_repo)
    (temp_git_repo / "task.py").write_text("print('hello')\n", encoding="utf-8")
    original_workflow = (
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py", "{{output}}/result.txt"]
      files:
        - localpath: ./task.py
          path: /workspace/task.py
""".strip()
        + "\n"
    )
    (temp_git_repo / "workflow.yaml").write_text(original_workflow, encoding="utf-8")

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        '[osmo]\nruntime_install_requirement = "roar-cli==9.9.9"\n',
        encoding="utf-8",
    )
    git_commit("enable osmo transparent workflow preparation")

    result = roar_cli("run", "./osmo", "workflow", "submit", "workflow.yaml")

    assert result.returncode == 0

    original_rendered = (temp_git_repo / "workflow.yaml").read_text(encoding="utf-8")
    submitted_rendered = (temp_git_repo / ".submitted-osmo-workflow.yaml").read_text(
        encoding="utf-8"
    )
    submitted_command = json.loads(
        (temp_git_repo / ".submitted-osmo-command.json").read_text(encoding="utf-8")
    )

    assert original_rendered == original_workflow
    assert submitted_command[2] != "workflow.yaml"
    assert Path(submitted_command[2]).resolve().parent == temp_git_repo.resolve()
    assert "path: /tmp/roar-osmo-wrapper.sh" in submitted_rendered
    assert "name: roar-lineage" in submitted_rendered
    assert "localpath: ./task.py" in submitted_rendered
    assert (
        '"$python_bin" -m pip install --disable-pip-version-check --no-input --target "$install_root" "roar-cli==9.9.9"'
        in submitted_rendered
    )
    assert "urlopen(tracer_url)" not in submitted_rendered
    assert "find_ptrace_tracer" in submitted_rendered
    receipt_dir = temp_git_repo / ".roar" / "osmo" / "submissions"
    receipt_path = max(receipt_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["osmo_submit"]["submit"]["workflow_spec"]["path"] == str(
        temp_git_repo / "workflow.yaml"
    )
    assert payload["osmo_submit"]["submit"]["prepared_workflow"]["workflow_spec"]["path"] == str(
        Path(submitted_command[2]).resolve()
    )
    assert payload["osmo_submit"]["submit"]["prepared_workflow"]["wrapped_tasks"] == ["basic"]
    assert (
        payload["osmo_submit"]["submit"]["prepared_workflow"]["runtime_install_requirement"]
        == "roar-cli==9.9.9"
    )
    assert (
        "runtime_tracer_download_url" not in payload["osmo_submit"]["submit"]["prepared_workflow"]
    )


def test_roar_run_osmo_workflow_submit_can_inject_local_install_artifact(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
) -> None:
    _write_fake_osmo(temp_git_repo)
    wheel_path = temp_git_repo / "dist" / "roar_cli.whl"
    wheel_path.parent.mkdir(parents=True, exist_ok=True)
    wheel_path.write_bytes(b"\xfcwheel")
    (temp_git_repo / "task.py").write_text("print('hello')\n", encoding="utf-8")
    (temp_git_repo / "workflow.yaml").write_text(
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
      files:
        - localpath: ./task.py
          path: /workspace/task.py
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        '[osmo]\nruntime_install_local_path = "dist/roar_cli.whl"\n',
        encoding="utf-8",
    )
    git_commit("enable osmo runtime install artifact")

    result = roar_cli("run", "./osmo", "workflow", "submit", "workflow.yaml")

    assert result.returncode == 0

    submitted_rendered = (temp_git_repo / ".submitted-osmo-workflow.yaml").read_text(
        encoding="utf-8"
    )
    receipt_dir = temp_git_repo / ".roar" / "osmo" / "submissions"
    receipt_path = max(receipt_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    prepared = payload["osmo_submit"]["submit"]["prepared_workflow"]

    assert f"localpath: {wheel_path}" in submitted_rendered
    assert "path: /tmp/roar-osmo-install.whl" in submitted_rendered
    assert (
        '"$python_bin" -m pip install --disable-pip-version-check --no-input --target "$install_root" "/tmp/roar-osmo-install.whl"'
        in submitted_rendered
    )
    assert prepared["runtime_install_local_path"] == str(wheel_path)
    assert prepared["runtime_install_remote_path"] == "/tmp/roar-osmo-install.whl"
    assert "runtime_tracer_download_url" not in prepared
    assert "runtime_tracer_remote_path" not in prepared
    assert "base64.b64decode(payload)" not in submitted_rendered
    assert "find_ptrace_tracer" in submitted_rendered
