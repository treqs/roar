from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from roar.backends.osmo import OsmoAttachOptions, attach_osmo_workflow
from roar.db.context import create_database_context


def test_attach_osmo_workflow_can_download_and_reconstitute_lineage(
    monkeypatch,
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
      outputs:
        - dataset:
            name: {{ output_dataset }}
            path: result.txt
        - dataset:
            name: roar-lineage
            path: roar-fragments.json

default-values:
  workflow_name: roar-osmo-attach
  output_dataset: roar-osmo-attach-output
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (roar_dir / "config.toml").write_text(
        "[osmo]\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n",
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
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-attach","status":"COMPLETED"}\n',
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
                            "job_uid": "osmo-attach-task",
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
                                    "path": "${ROAR_PROJECT_DIR}/outputs/attach-output.txt",
                                    "hash": "attachoutputhash",
                                    "hash_algorithm": "blake3",
                                    "size": 18,
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
                (target_dir / "result.txt").write_text("ROAR_OSMO_ATTACH_OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="downloaded\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = attach_osmo_workflow(
        roar_dir=roar_dir,
        repo_root=str(repo_root),
        workflow_id="workflow-attach",
        options=OsmoAttachOptions(
            workflow_spec_argument="workflow.yaml",
            workflow_spec_path="workflow.yaml",
            set_strings={
                "workflow_name": "workflow-attach",
                "output_dataset": "workflow-attach-output",
            },
        ),
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_attach"]["workflow_id"] == "workflow-attach"
    assert metadata["osmo_attach"]["workflow_status"] == "COMPLETED"
    assert metadata["osmo_attach"]["lineage_reconstitution"]["fragments_processed"] == 1
    assert metadata["osmo_attach"]["lineage_reconstitution"]["jobs_merged"] == 1
    assert metadata["osmo_attach"]["downloaded_outputs"][0]["dataset_name"] == (
        "workflow-attach-output"
    )

    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    assert any(path.name == "query-COMPLETED.json" for path in output_paths)
    assert any(path.name == "result.txt" for path in output_paths)
    assert any(path.name == "roar-fragments.json" for path in output_paths)
    receipt_path = next(path for path in output_paths if "attachments" in str(path))
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload["osmo_attach"]["workflow_id"] == "workflow-attach"

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

    assert len(child_jobs) == 1
    assert child_jobs[0]["execution_backend"] == "osmo"
    assert child_jobs[0]["execution_role"] == "task"
    assert child_jobs[0]["command"] == "osmo_task:basic"


def test_attach_osmo_workflow_supports_dataset_hints_without_workflow_spec(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text("workflow: {}\n", encoding="utf-8")
    (roar_dir / "config.toml").write_text(
        "[osmo]\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n",
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
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-attach-hints","status":"COMPLETED"}\n',
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
                            "job_uid": "osmo-attach-hints-task",
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
                                    "path": "${ROAR_PROJECT_DIR}/outputs/hints-output.txt",
                                    "hash": "hintsoutputhash",
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
                (target_dir / "result.txt").write_text("ROAR_OSMO_HINTS_OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="downloaded\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = attach_osmo_workflow(
        roar_dir=roar_dir,
        repo_root=str(repo_root),
        workflow_id="workflow-attach-hints",
        options=OsmoAttachOptions(
            dataset_names=["workflow-attach-hints-output", "roar-lineage"],
        ),
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_attach"]["attach"]["dataset_hints"] == [
        "workflow-attach-hints-output",
        "roar-lineage",
    ]
    assert metadata["osmo_attach"]["downloaded_outputs"][0]["dataset_name"] == (
        "workflow-attach-hints-output"
    )
    assert metadata["osmo_attach"]["lineage_reconstitution"]["fragments_processed"] == 1


def test_attach_osmo_workflow_uses_configured_lineage_dataset_name_without_hints(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    workflow_path = repo_root / "workflow.yaml"
    workflow_path.write_text("workflow: {}\n", encoding="utf-8")
    (roar_dir / "config.toml").write_text(
        "[osmo]\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n"
        'lineage_bundle_dataset_name = "roar-lineage"\n',
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
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-attach-config","status":"COMPLETED"}\n',
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
                            "job_uid": "osmo-attach-config-task",
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
                                    "path": "${ROAR_PROJECT_DIR}/outputs/config-attach-output.txt",
                                    "hash": "configattachhash",
                                    "hash_algorithm": "blake3",
                                    "size": 24,
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
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="downloaded\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run command: {command!r}")

    monkeypatch.setattr(subprocess, "run", _run)

    result = attach_osmo_workflow(
        roar_dir=roar_dir,
        repo_root=str(repo_root),
        workflow_id="workflow-attach-config",
        options=OsmoAttachOptions(),
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_attach"]["attach"]["dataset_hints"] == ["roar-lineage"]
    assert metadata["osmo_attach"]["lineage_reconstitution"]["fragments_processed"] == 1
    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    assert any(path.name == "roar-fragments.json" for path in output_paths)


def test_attach_osmo_workflow_supports_task_hints_without_workflow_spec(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    def _run(command, *args, **kwargs):
        del args, kwargs
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="deadbeef\n",
                stderr="",
            )
        if command[1:3] == ["workflow", "query"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"name":"workflow-attach-failed","status":"FAILED"}\n',
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

    result = attach_osmo_workflow(
        roar_dir=roar_dir,
        repo_root=str(repo_root),
        workflow_id="workflow-attach-failed",
        options=OsmoAttachOptions(task_names=["basic"]),
    )

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get(result.job_id)

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert result.exit_code == 0
    assert metadata["osmo_attach"]["attach"]["task_name_hints"] == ["basic"]
    assert metadata["osmo_attach"]["workflow_status"] == "FAILED"
    assert metadata["osmo_attach"]["workflow_diagnostics"]["task_logs"][0]["task_name"] == "basic"
    output_paths = {Path(str(entry["path"])) for entry in result.outputs}
    log_path = next(path for path in output_paths if path.name == "basic.log")
    assert "task failed" in log_path.read_text(encoding="utf-8")
