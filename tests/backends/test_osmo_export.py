from __future__ import annotations

import json
import time
from pathlib import Path

from roar.backends.osmo import export_osmo_lineage_bundle
from roar.db.context import create_database_context


def test_export_osmo_lineage_bundle_serializes_latest_job_as_fragment(temp_git_repo: Path) -> None:
    input_path = temp_git_repo / "inputs" / "source.txt"
    output_path = temp_git_repo / "outputs" / "result.txt"
    bundle_path = temp_git_repo / "roar-fragments.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("source\n", encoding="utf-8")
    output_path.write_text("result\n", encoding="utf-8")

    with create_database_context(temp_git_repo / ".roar") as db_ctx:
        db_ctx.job_recording.record_job(
            command="python task.py",
            timestamp=time.time(),
            job_uid="local-job-uid",
            duration_seconds=1.25,
            exit_code=0,
            input_files=[str(input_path)],
            output_files=[str(output_path)],
            execution_backend="local",
            execution_role="host",
            repo_root=str(temp_git_repo),
        )

    exported = export_osmo_lineage_bundle(
        roar_dir=temp_git_repo / ".roar",
        output_path=bundle_path,
        task_id="osmo-task-1",
        task_name="basic",
    )

    assert exported.exported_job_uid == "local-job-uid"
    assert exported.fragment_count == 1
    assert exported.task_id == "osmo-task-1"
    assert exported.task_name == "basic"

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["exported_job_uid"] == "local-job-uid"
    fragment = payload["fragments"][0]
    assert fragment["backend"] == "osmo"
    assert fragment["job_uid"] == "local-job-uid"
    assert fragment["task_id"] == "osmo-task-1"
    assert fragment["task_name"] == "basic"
    assert fragment["backend_metadata"]["execution_role"] == "task"
    assert fragment["backend_metadata"]["source_execution_backend"] == "local"
    assert fragment["reads"][0]["path"] == "${ROAR_PROJECT_DIR}/inputs/source.txt"
    assert fragment["writes"][0]["path"] == "${ROAR_PROJECT_DIR}/outputs/result.txt"


def test_export_osmo_lineage_bundle_can_select_job_uid(temp_git_repo: Path) -> None:
    first_output = temp_git_repo / "outputs" / "first.txt"
    second_output = temp_git_repo / "outputs" / "second.txt"
    first_output.parent.mkdir(parents=True, exist_ok=True)
    first_output.write_text("first\n", encoding="utf-8")
    second_output.write_text("second\n", encoding="utf-8")

    with create_database_context(temp_git_repo / ".roar") as db_ctx:
        db_ctx.job_recording.record_job(
            command="python first.py",
            timestamp=time.time() - 10,
            job_uid="first-job",
            duration_seconds=0.5,
            exit_code=0,
            output_files=[str(first_output)],
            execution_backend="local",
            execution_role="host",
            repo_root=str(temp_git_repo),
        )
        db_ctx.job_recording.record_job(
            command="python second.py",
            timestamp=time.time(),
            job_uid="second-job",
            duration_seconds=0.5,
            exit_code=0,
            output_files=[str(second_output)],
            execution_backend="local",
            execution_role="host",
            repo_root=str(temp_git_repo),
        )

    bundle_path = temp_git_repo / "selected.json"
    exported = export_osmo_lineage_bundle(
        roar_dir=temp_git_repo / ".roar",
        output_path=bundle_path,
        job_uid="first-job",
        task_name="selected-task",
    )

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert exported.exported_job_uid == "first-job"
    assert payload["metadata"]["exported_job_uid"] == "first-job"
    assert payload["fragments"][0]["writes"][0]["path"] == "${ROAR_PROJECT_DIR}/outputs/first.txt"
    assert payload["fragments"][0]["task_name"] == "selected-task"

