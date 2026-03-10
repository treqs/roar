"""Cloud-demo-shaped Ray lineage repro through `roar run ray job submit ...`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import (
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(240)]


def _parse_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("script") == "cloud_demo_like":
            return payload
    raise AssertionError(f"Unable to parse cloud-demo-like payload from output:\n{stdout}")


def _run_pipeline(project_dir: Path, ray_cluster: dict[str, str]) -> dict[str, object]:
    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "cloud_demo_like/main.py",
        use_fragment_store=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return _parse_payload(result.stdout)


def test_cloud_demo_like_pipeline_produces_phase_task_lineage(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("cloud-demo-like")
    init_host_project(project_dir)

    payload = _run_pipeline(project_dir, ray_cluster)
    run_id = str(payload.get("run_id") or "")
    report_key = str(payload.get("report_key") or "")

    assert run_id, payload
    assert report_key.endswith("/results/final_report.json"), payload

    report_rows = query_roar_db(
        project_dir,
        """
        SELECT COALESCE(a.path, a.first_seen_path) AS path
        FROM artifacts a
        WHERE COALESCE(a.path, a.first_seen_path) LIKE ?
        """,
        (f"%cloud-demo-like/{run_id}/results/final_report.json",),
    )
    assert report_rows, "Expected the cloud-demo-like pipeline report artifact in lineage"

    job_rows = query_roar_db(
        project_dir,
        """
        SELECT command, step_number
        FROM jobs
        WHERE job_type = 'ray_task'
        ORDER BY step_number, command
        """,
    )
    commands = {str(row.get("command") or "") for row in job_rows}

    expected_commands = {
        "ray_task:extract_shard",
        "ray_task:train_on_shard",
        "ray_task:evaluate_shard",
    }
    missing = sorted(expected_commands.difference(commands))
    assert not missing, (
        "Expected cloud-demo-shaped lineage to include first-class Ray task families "
        f"for extraction, training, and evaluation, but missing={missing}. "
        f"Observed commands={sorted(commands)}"
    )
