from __future__ import annotations

import json

import pytest

from tests.e2e.ray.conftest import (
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


def _parse_json_line(stdout: str) -> dict[str, str]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items()}
    return {}


def test_host_submit_worker_bootstrap_reconstitutes_worker_file_lineage(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("worker-bootstrap")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "worker_bootstrap_probe.py",
        use_fragment_store=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = _parse_json_line(result.stdout)
    assert payload, f"Expected JSON payload in stdout, got:\n{result.stdout}"
    assert payload["body"] == "worker bootstrap probe\n"
    assert payload["aws_endpoint_url"].startswith("http://127.0.0.1:"), payload

    rows = query_roar_db(
        project_dir,
        """
        SELECT j.script,
               json_extract(j.metadata, '$.ray_task_id') AS ray_task_id,
               COALESCE(a.path, a.first_seen_path) AS path,
               a.capture_method
        FROM jobs j
        JOIN job_outputs jo ON jo.job_id = j.id
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE j.job_type = 'ray_task'
          AND COALESCE(a.path, a.first_seen_path) LIKE ?
        ORDER BY j.id
        """,
        ("%/artifacts/worker_bootstrap_probe/output.txt",),
    )

    assert rows, "Expected worker bootstrap probe output in the reconstituted roar.db"
    assert all(str(row.get("capture_method") or "") for row in rows), rows
    assert all(str(row.get("script") or "").endswith("._probe") for row in rows), rows
    assert all(str(row.get("ray_task_id") or "") for row in rows), rows
