from __future__ import annotations

import json

import pytest

from tests.backends.ray.e2e.conftest import (
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


def _parse_payload(stdout: str) -> dict[str, str]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items()}
    raise AssertionError(f"Expected JSON payload in stdout, got:\n{stdout}")


def test_nested_subprocess_ray_task_keeps_s3_lineage_out_of_proxy_only_bucket(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("nested-subprocess-s3")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "nested_subprocess_s3_lineage.py",
        use_fragment_store=True,
        timeout=240,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = _parse_payload(result.stdout)
    output_uri = str(payload.get("output_uri") or "")
    assert output_uri.startswith("s3://test-bucket/nested-subprocess/"), payload

    rows = query_roar_db(
        project_dir,
        """
        SELECT j.command,
               j.script,
               json_extract(j.metadata, '$.ray_task_id') AS ray_task_id,
               COALESCE(a.path, a.first_seen_path) AS path,
               a.capture_method
        FROM jobs j
        JOIN job_outputs jo ON jo.job_id = j.id
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE j.job_type = 'ray_task'
          AND COALESCE(a.path, a.first_seen_path) = ?
        ORDER BY j.command
        """,
        (output_uri,),
    )

    assert rows, f"Expected reconstituted S3 output row for {output_uri}"
    assert any(str(row.get("command") or "") == "ray_task:write_s3" for row in rows), rows
