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
                    "job_uid": "osmo-attach-product-task",
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
                            "path": "${ROAR_PROJECT_DIR}/outputs/attach-product-output.txt",
                            "hash": "attachproducthash",
                            "hash_algorithm": "blake3",
                            "size": 25,
                            "capture_method": "python",
                        }
                    ],
                    "backend_metadata": {"execution_role": "task"},
                }
            ]
        }
        (target / "roar-fragments.json").write_text(json.dumps(payload), encoding="utf-8")
    else:
        (target / "result.txt").write_text("ROAR_OSMO_ATTACH_OK\\n", encoding="utf-8")
    raise SystemExit(0)

print(f"unexpected args: {args!r}", file=sys.stderr)
raise SystemExit(2)
"""
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_roar_osmo_attach_records_receipt_and_reconstitutes_lineage(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    _write_fake_osmo(temp_git_repo)
    (temp_git_repo / "workflow.yaml").write_text(
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
  workflow_name: workflow-attach-product
  output_dataset: workflow-attach-product-output
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        "[osmo]\ndownload_declared_outputs = true\ningest_lineage_bundles = true\n",
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "attach",
        "workflow-attach-product",
        "--osmo-binary",
        "./osmo",
        "--workflow-spec",
        "workflow.yaml",
        "--set-string",
        "workflow_name=workflow-attach-product",
        "--set-string",
        "output_dataset=workflow-attach-product-output",
    )

    assert result.returncode == 0

    db_path = temp_git_repo / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT id, job_uid, execution_backend, execution_role, exit_code
            FROM jobs
            WHERE execution_role = 'attach'
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
        output = conn.execute(
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

    assert job is not None
    assert job["execution_backend"] == "osmo"
    assert job["execution_role"] == "attach"
    assert job["exit_code"] == 0
    assert len(child_jobs) == 1
    assert child_jobs[0]["parent_job_uid"] == job["job_uid"]
    assert child_jobs[0]["execution_role"] == "task"
    assert child_jobs[0]["command"] == "osmo_task:basic"

    output_paths = [Path(str(row["path"])) for row in output]
    receipt_path = next(path for path in output_paths if "attachments" in str(path))
    query_path = next(path for path in output_paths if path.name == "query-COMPLETED.json")
    downloaded_result = next(path for path in output_paths if path.name == "result.txt")
    downloaded_bundle = next(path for path in output_paths if path.name == "roar-fragments.json")
    assert (
        receipt_path
        == temp_git_repo
        / ".roar"
        / "osmo"
        / "attachments"
        / "workflow-attach-product-COMPLETED.json"
    )
    assert query_path.exists()
    assert downloaded_result.read_text(encoding="utf-8").strip() == "ROAR_OSMO_ATTACH_OK"
    assert downloaded_bundle.exists()

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["osmo_attach"]["workflow_id"] == "workflow-attach-product"
    assert payload["osmo_attach"]["workflow_status"] == "COMPLETED"
    assert payload["osmo_attach"]["lineage_reconstitution"]["fragments_processed"] == 1
    assert payload["osmo_attach"]["lineage_reconstitution"]["jobs_merged"] == 1


def test_roar_osmo_attach_supports_dataset_hints_without_workflow_spec(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    _write_fake_osmo(temp_git_repo)
    (temp_git_repo / "workflow.yaml").write_text("workflow: {}\n", encoding="utf-8")

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        "[osmo]\ndownload_declared_outputs = true\ningest_lineage_bundles = true\n",
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "attach",
        "workflow-attach-product",
        "--osmo-binary",
        "./osmo",
        "--dataset",
        "workflow-attach-product-output",
        "--dataset",
        "roar-lineage",
    )

    assert result.returncode == 0

    db_path = temp_git_repo / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT id, metadata
            FROM jobs
            WHERE execution_role = 'attach'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_attach"]["attach"]["dataset_hints"] == [
        "workflow-attach-product-output",
        "roar-lineage",
    ]
    assert metadata["osmo_attach"]["downloaded_outputs"][0]["dataset_name"] == (
        "workflow-attach-product-output"
    )
    assert metadata["osmo_attach"]["lineage_reconstitution"]["fragments_processed"] == 1


def test_roar_osmo_attach_uses_configured_lineage_dataset_name_without_hints(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    _write_fake_osmo(temp_git_repo)
    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        "[osmo]\n"
        "download_declared_outputs = true\n"
        "ingest_lineage_bundles = true\n"
        'lineage_bundle_dataset_name = "roar-lineage"\n',
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "attach",
        "workflow-attach-product",
        "--osmo-binary",
        "./osmo",
    )

    assert result.returncode == 0

    db_path = temp_git_repo / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT id, metadata
            FROM jobs
            WHERE execution_role = 'attach'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert job is not None
    metadata = json.loads(str(job["metadata"]))
    assert metadata["osmo_attach"]["attach"]["dataset_hints"] == ["roar-lineage"]
    assert metadata["osmo_attach"]["lineage_reconstitution"]["fragments_processed"] == 1
