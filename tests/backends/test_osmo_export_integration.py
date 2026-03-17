from __future__ import annotations

import json
import time
from pathlib import Path

from roar.db.context import create_database_context


def test_roar_osmo_export_lineage_bundle_writes_bundle_for_latest_job(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    output_path = temp_git_repo / "outputs" / "result.txt"
    bundle_path = temp_git_repo / "dist" / "roar-fragments.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("result\n", encoding="utf-8")

    with create_database_context(temp_git_repo / ".roar") as db_ctx:
        db_ctx.job_recording.record_job(
            command="python worker.py",
            timestamp=time.time(),
            job_uid="remote-job",
            duration_seconds=2.0,
            exit_code=0,
            output_files=[str(output_path)],
            execution_backend="local",
            execution_role="host",
            repo_root=str(temp_git_repo),
        )

    result = roar_cli(
        "osmo",
        "export-lineage-bundle",
        "dist/roar-fragments.json",
        "--task-id",
        "osmo-task-remote",
        "--task-name",
        "basic",
    )

    assert result.returncode == 0
    assert "dist/roar-fragments.json" in result.stdout

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["exported_job_uid"] == "remote-job"
    assert payload["metadata"]["task_id"] == "osmo-task-remote"
    assert payload["fragments"][0]["task_name"] == "basic"
    assert payload["fragments"][0]["writes"][0]["path"] == "${ROAR_PROJECT_DIR}/outputs/result.txt"
