from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from roar.backends.osmo.host_execution import execute_osmo_workflow_submit
from roar.core.models.run import RunContext
from roar.db.context import create_database_context
from roar.execution.runtime.host_execution import ExecutionSetupError


def test_execute_osmo_workflow_submit_records_local_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    completed = subprocess.CompletedProcess(
        args=["osmo", "workflow", "submit", "workflow.yaml", "--format-type", "json"],
        returncode=0,
        stdout='{"name":"workflow-123","overview":"https://osmo.example/workflows/123"}\n',
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=["osmo", "workflow", "submit", "workflow.yaml", "--format-type", "json"],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    assert job["execution_backend"] == "osmo"
    assert job["execution_role"] == "submit"
    assert job["job_type"] == "run"
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_submit"]["workflow_id"] == "workflow-123"
    assert metadata["osmo_submit"]["response"]["overview"] == "https://osmo.example/workflows/123"
    assert result.exit_code == 0
    assert result.inputs == []
    assert len(result.outputs) == 1
    receipt_path = Path(str(result.outputs[0]["path"]))
    assert receipt_path.exists()
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload["osmo_submit"]["workflow_id"] == "workflow-123"
    assert receipt_path.name == "workflow-123.json"

    captured = capsys.readouterr()
    assert '"name":"workflow-123"' in captured.out
    assert captured.err == ""


def test_execute_osmo_workflow_submit_records_text_output_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    completed = subprocess.CompletedProcess(
        args=["osmo", "workflow", "submit", "workflow.yaml"],
        returncode=7,
        stdout="submitted workflow workflow-456\n",
        stderr="permission denied\n",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=["osmo", "workflow", "submit", "workflow.yaml"],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_submit"]["workflow_id"] is None
    assert metadata["osmo_submit"]["response_format"] == "text"
    assert metadata["osmo_submit"]["stdout"] == "submitted workflow workflow-456"
    assert metadata["osmo_submit"]["stderr"] == "permission denied"
    assert result.exit_code == 7
    assert len(result.outputs) == 1
    receipt_path = Path(str(result.outputs[0]["path"]))
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload["osmo_submit"]["return_code"] == 7
    assert receipt_path.name == "submit.json"

    captured = capsys.readouterr()
    assert "submitted workflow workflow-456" in captured.out
    assert "permission denied" in captured.err


def test_execute_osmo_workflow_submit_records_workflow_and_set_file_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text(
        """
workflow:
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    dataset_path = repo_root / "params.json"
    dataset_path.write_text('{"epochs": 10}\n', encoding="utf-8")

    completed = subprocess.CompletedProcess(
        args=[
            "osmo",
            "workflow",
            "submit",
            "workflow.yaml",
            "--set-file",
            "params=params.json",
            "--set-string",
            "mode=test",
            "--format-type",
            "json",
        ],
        returncode=0,
        stdout='{"name":"workflow-inputs"}\n',
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=[
                "osmo",
                "workflow",
                "submit",
                "workflow.yaml",
                "--set-file",
                "params=params.json",
                "--set-string",
                "mode=test",
                "--format-type",
                "json",
            ],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_submit"]["submit"]["workflow_spec"]["argument"] == "workflow.yaml"
    assert metadata["osmo_submit"]["submit"]["workflow_spec"]["path"] == str(workflow_path)
    assert metadata["osmo_submit"]["submit"]["set_files"] == {"params": "params.json"}
    assert metadata["osmo_submit"]["submit"]["set_strings"] == {"mode": "test"}

    input_paths = {str(entry["path"]) for entry in result.inputs}
    assert input_paths == {str(workflow_path), str(dataset_path)}


def test_execute_osmo_workflow_submit_can_wait_for_workflow_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        "[osmo]\nwait_for_completion = true\npoll_interval_seconds = 0.01\nquery_timeout_seconds = 1\n",
        encoding="utf-8",
    )

    def _run(command, *args, **kwargs):
        del args, kwargs
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="deadbeef\n",
                stderr="",
            )
        if command[1:3] == ["workflow", "submit"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-123"}\n',
                stderr="",
            )
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-123","status":"COMPLETED"}\n',
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=["osmo", "workflow", "submit", "workflow.yaml", "--format-type", "json"],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert result.exit_code == 0
    assert metadata["osmo_submit"]["wait_for_completion"] is True
    assert metadata["osmo_submit"]["workflow_status"] == "COMPLETED"
    assert metadata["osmo_submit"]["workflow_query"]["status"] == "COMPLETED"
    assert len(result.outputs) == 2
    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    query_path = next(path for path in output_paths if "diagnostics" in str(path))
    receipt_path = next(path for path in output_paths if "submissions" in str(path))
    assert query_path.name == "query-COMPLETED.json"
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload["osmo_submit"]["workflow_status"] == "COMPLETED"
    assert receipt_path.name == "workflow-123-COMPLETED.json"
    assert metadata["osmo_submit"]["workflow_diagnostics"]["query_artifact_path"] == str(query_path)

    captured = capsys.readouterr()
    assert "waiting for OSMO workflow workflow-123" in captured.err
    assert "finished with status COMPLETED" in captured.err


def test_execute_osmo_workflow_submit_can_download_declared_dataset_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text(
        """
workflow:
  name: {{ workflow_name }}
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
      outputs:
        - dataset:
            name: {{ output_dataset }}
            path: result.txt

default-values:
  workflow_name: roar-osmo-basic
  output_dataset: roar-osmo-basic-output
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (roar_dir / "config.toml").write_text(
        "[osmo]\nwait_for_completion = true\ndownload_declared_outputs = true\npoll_interval_seconds = 0.01\nquery_timeout_seconds = 1\n",
        encoding="utf-8",
    )

    def _run(command, *args, **kwargs):
        del args, kwargs
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="deadbeef\n",
                stderr="",
            )
        if command[1:3] == ["workflow", "submit"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-download"}\n',
                stderr="",
            )
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-download","status":"COMPLETED"}\n',
                stderr="",
            )
        if command[1:3] == ["dataset", "download"]:
            target_dir = Path(command[-1])
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "result.txt").write_text("ROAR_OSMO_BASIC_OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="downloaded\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=[
                "osmo",
                "workflow",
                "submit",
                "workflow.yaml",
                "--set-string",
                "workflow_name=workflow-download",
                "--set-string",
                "output_dataset=workflow-download-output",
                "--format-type",
                "json",
            ],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert result.exit_code == 0
    assert metadata["osmo_submit"]["download_declared_outputs"] is True
    assert metadata["osmo_submit"]["downloaded_outputs"][0]["dataset_name"] == (
        "workflow-download-output"
    )
    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    assert any(path.name == "result.txt" for path in output_paths)
    assert any(path.name == "query-COMPLETED.json" for path in output_paths)
    assert any(path.name == "workflow-download-COMPLETED.json" for path in output_paths)


def test_execute_osmo_workflow_submit_can_reconstitute_downloaded_lineage_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text(
        """
workflow:
  name: {{ workflow_name }}
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
      outputs:
        - dataset:
            name: {{ output_dataset }}
            path: result.txt
        - dataset:
            name: roar-lineage
            path: roar-fragments.json

default-values:
  workflow_name: roar-osmo-lineage
  output_dataset: roar-osmo-lineage-output
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (roar_dir / "config.toml").write_text(
        "[osmo]\n"
        "wait_for_completion = true\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n"
        "poll_interval_seconds = 0.01\n"
        "query_timeout_seconds = 1\n",
        encoding="utf-8",
    )

    def _run(command, *args, **kwargs):
        del args, kwargs
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="deadbeef\n",
                stderr="",
            )
        if command[1:3] == ["workflow", "submit"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-lineage"}\n',
                stderr="",
            )
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-lineage","status":"COMPLETED"}\n',
                stderr="",
            )
        if command[1:3] == ["dataset", "download"]:
            dataset_ref = command[3]
            target_dir = Path(command[-1])
            target_dir.mkdir(parents=True, exist_ok=True)
            if dataset_ref.startswith("roar-lineage:"):
                payload = {
                    "fragments": [
                        {
                            "job_uid": "osmo-task-basic",
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
                                    "size": workflow_path.stat().st_size,
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
                (target_dir / "roar-fragments.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            else:
                (target_dir / "result.txt").write_text("ROAR_OSMO_BASIC_OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="downloaded\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=[
                "osmo",
                "workflow",
                "submit",
                "workflow.yaml",
                "--set-string",
                "workflow_name=workflow-lineage",
                "--set-string",
                "output_dataset=workflow-lineage-output",
                "--format-type",
                "json",
            ],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    conn = sqlite3.connect(roar_dir / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        child_jobs = conn.execute(
            """
            SELECT id, parent_job_uid, execution_backend, execution_role, command
            FROM jobs
            WHERE parent_job_uid = ? AND job_type = 'osmo_task'
            ORDER BY id ASC
            """,
            (result.job_uid,),
        ).fetchall()
    finally:
        conn.close()

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    lineage = metadata["osmo_submit"]["lineage_reconstitution"]
    assert lineage["fragments_processed"] == 1
    assert lineage["jobs_merged"] == 1
    assert lineage["bundle_count"] == 1
    assert lineage["bundles"][0]["dataset_name"] == "roar-lineage"

    assert len(child_jobs) == 1
    child_job = child_jobs[0]
    assert child_job["execution_backend"] == "osmo"
    assert child_job["execution_role"] == "task"
    assert child_job["parent_job_uid"] == result.job_uid
    assert child_job["command"] == "osmo_task:basic"

    with create_database_context(roar_dir) as db_ctx:
        child_outputs = db_ctx.jobs.get_outputs(int(child_job["id"]))
        child_inputs = db_ctx.jobs.get_inputs(int(child_job["id"]))

    assert [entry["path"] for entry in child_inputs] == [str(workflow_path)]
    assert [entry["path"] for entry in child_outputs] == [
        str(repo_root / "outputs" / "worker-output.txt")
    ]

    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    assert any(path.name == "roar-fragments.json" for path in output_paths)
    assert any(path.name == "result.txt" for path in output_paths)
    assert any(path.name == "query-COMPLETED.json" for path in output_paths)
    assert any(path.name == "workflow-lineage-COMPLETED.json" for path in output_paths)


def test_execute_osmo_workflow_submit_uses_configured_lineage_dataset_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text(
        """
workflow:
  name: {{ workflow_name }}
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
      outputs:
        - dataset:
            name: {{ output_dataset }}
            path: result.txt

default-values:
  workflow_name: roar-osmo-config-lineage
  output_dataset: roar-osmo-config-lineage-output
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (roar_dir / "config.toml").write_text(
        "[osmo]\n"
        "wait_for_completion = true\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n"
        'lineage_bundle_dataset_name = "roar-lineage"\n'
        "poll_interval_seconds = 0.01\n"
        "query_timeout_seconds = 1\n",
        encoding="utf-8",
    )

    def _run(command, *args, **kwargs):
        del args, kwargs
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="deadbeef\n",
                stderr="",
            )
        if command[1:3] == ["workflow", "submit"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-config-lineage"}\n',
                stderr="",
            )
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-config-lineage","status":"COMPLETED"}\n',
                stderr="",
            )
        if command[1:3] == ["dataset", "download"]:
            dataset_ref = command[3]
            target_dir = Path(command[-1])
            target_dir.mkdir(parents=True, exist_ok=True)
            if dataset_ref.startswith("roar-lineage:"):
                payload = {
                    "fragments": [
                        {
                            "job_uid": "osmo-config-lineage-task",
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
                                    "size": workflow_path.stat().st_size,
                                    "capture_method": "python",
                                }
                            ],
                            "writes": [
                                {
                                    "path": "${ROAR_PROJECT_DIR}/outputs/config-lineage-output.txt",
                                    "hash": "configlineagehash",
                                    "hash_algorithm": "blake3",
                                    "size": 25,
                                    "capture_method": "python",
                                }
                            ],
                            "backend_metadata": {"execution_role": "task"},
                        }
                    ]
                }
                (target_dir / "roar-fragments.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            else:
                (target_dir / "result.txt").write_text("ROAR_OSMO_CONFIG_OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="downloaded\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=[
                "osmo",
                "workflow",
                "submit",
                "workflow.yaml",
                "--set-string",
                "workflow_name=workflow-config-lineage",
                "--set-string",
                "output_dataset=workflow-config-lineage-output",
                "--format-type",
                "json",
            ],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_submit"]["submit"]["dataset_hints"] == ["roar-lineage"]
    assert metadata["osmo_submit"]["lineage_reconstitution"]["fragments_processed"] == 1
    downloaded_names = [item["dataset_name"] for item in metadata["osmo_submit"]["downloaded_outputs"]]
    assert downloaded_names == ["workflow-config-lineage-output", "roar-lineage"]


def test_execute_osmo_workflow_submit_fails_when_waited_workflow_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text(
        """
workflow:
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
      outputs: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (roar_dir / "config.toml").write_text(
        "[osmo]\nwait_for_completion = true\npoll_interval_seconds = 0.01\nquery_timeout_seconds = 1\n",
        encoding="utf-8",
    )

    def _run(command, *args, **kwargs):
        del args, kwargs
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="deadbeef\n",
                stderr="",
            )
        if command[1:3] == ["workflow", "submit"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-999"}\n',
                stderr="",
            )
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-999","status":"FAILED"}\n',
                stderr="",
            )
        if command[1:3] == ["workflow", "logs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="task failed\n",
                stderr="traceback line\n",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = execute_osmo_workflow_submit(
        RunContext(
            roar_dir=roar_dir,
            repo_root=str(repo_root),
            command=["osmo", "workflow", "submit", "workflow.yaml", "--format-type", "json"],
            execution_backend="osmo",
            execution_role="submit",
        )
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert result.exit_code == 1
    assert job["exit_code"] == 1
    assert metadata["osmo_submit"]["submit_return_code"] == 0
    assert metadata["osmo_submit"]["workflow_status"] == "FAILED"
    assert len(result.outputs) == 3
    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    query_path = next(path for path in output_paths if path.name == "query-FAILED.json")
    log_path = next(path for path in output_paths if path.name == "basic.log")
    receipt_path = next(path for path in output_paths if "submissions" in str(path))
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload["osmo_submit"]["workflow_status"] == "FAILED"
    assert receipt_path.name == "workflow-999-FAILED.json"
    assert metadata["osmo_submit"]["workflow_diagnostics"]["query_artifact_path"] == str(query_path)
    assert metadata["osmo_submit"]["workflow_diagnostics"]["task_logs"][0]["path"] == str(log_path)
    assert "task failed" in log_path.read_text(encoding="utf-8")

    captured = capsys.readouterr()
    assert "finished with status FAILED" in captured.err


def test_execute_osmo_workflow_submit_raises_setup_error_when_osmo_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    def _raise_missing(*args, **kwargs):
        raise FileNotFoundError("osmo")

    monkeypatch.setattr(subprocess, "run", _raise_missing)

    with pytest.raises(ExecutionSetupError, match="osmo CLI not found"):
        execute_osmo_workflow_submit(
            RunContext(
                roar_dir=roar_dir,
                repo_root=str(repo_root),
                command=["osmo", "workflow", "submit", "workflow.yaml"],
                execution_backend="osmo",
                execution_role="submit",
            )
        )
