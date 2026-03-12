"""Ray contract: driver-local S3 proxy fragments reconstitute on host submit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import (
    decrypt_fragment_batches,
    fetch_fragment_batches,
    init_host_project,
    load_fragment_key,
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


def _fragment_entries_for_key(
    fragments: list[dict[str, object]],
    *,
    key_suffix: str,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    expected_path = f"s3://test-bucket/{key_suffix}"
    for fragment in fragments:
        for io_kind in ("reads", "writes"):
            refs = fragment.get(io_kind, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                if str(ref.get("path") or "") != expected_path:
                    continue
                matches.append(
                    {
                        "io_kind": io_kind,
                        "ray_task_id": fragment.get("ray_task_id"),
                        "function_name": fragment.get("function_name"),
                        **ref,
                    }
                )
    return matches


def _proxy_rows_for_key(project_dir: Path, *, key_suffix: str) -> list[dict[str, object]]:
    return query_roar_db(
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
        ORDER BY j.id
        """,
        (f"s3://test-bucket/{key_suffix}",),
    )


def test_host_submit_reconstitutes_driver_proxy_fragment(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-driver-proxy")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "driver_proxy_capture.py",
        use_fragment_store=True,
    )

    assert result.returncode == 0, (
        f"submit failed (rc={result.returncode})\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )

    payload = _parse_json_line(result.stdout)
    assert payload, f"Expected JSON payload in stdout, got:\n{result.stdout}"
    assert payload.get("run_id"), payload
    assert payload.get("body") == f"driver proxy capture {payload['run_id']}\n", payload
    assert payload.get("aws_endpoint_url", "").startswith("http://127.0.0.1:"), payload
    assert payload.get("roar_proxy_port"), payload

    key_suffix = payload["key"]
    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    fragments = decrypt_fragment_batches(batches, key_payload["token"])

    refs = _fragment_entries_for_key(fragments, key_suffix=key_suffix)
    proxy_refs = [ref for ref in refs if str(ref.get("capture_method") or "") == "proxy"]

    assert proxy_refs, "Expected proxy fragment refs for the driver-only S3 artifact"
    assert {str(ref.get("ray_task_id") or "") for ref in proxy_refs} == {"proxy:driver"}, proxy_refs
    assert {str(ref.get("function_name") or "") for ref in proxy_refs} == {"s3_driver_proxy"}, (
        proxy_refs
    )

    rows = _proxy_rows_for_key(project_dir, key_suffix=key_suffix)
    assert rows, "Expected driver proxy artifact in the reconstituted roar.db"
    assert {str(row.get("capture_method") or "") for row in rows} == {"proxy"}, rows
    assert {str(row.get("ray_task_id") or "") for row in rows} == {"proxy:driver"}, rows
    assert {str(row.get("script") or "") for row in rows} == {"s3_driver_proxy"}, rows


def test_repeated_host_submit_uses_distinct_driver_proxy_ports_and_captures_both_runs(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-driver-proxy-repeat")
    init_host_project(project_dir)

    first = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "driver_proxy_capture.py",
        use_fragment_store=True,
    )
    second = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "driver_proxy_capture.py",
        use_fragment_store=True,
    )

    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 0, second.stderr or second.stdout

    first_payload = _parse_json_line(first.stdout)
    second_payload = _parse_json_line(second.stdout)
    assert first_payload.get("run_id"), first_payload
    assert second_payload.get("run_id"), second_payload
    assert first_payload.get("body") == f"driver proxy capture {first_payload['run_id']}\n", (
        first_payload
    )
    assert second_payload.get("body") == f"driver proxy capture {second_payload['run_id']}\n", (
        second_payload
    )
    assert first_payload.get("roar_proxy_port"), first_payload
    assert second_payload.get("roar_proxy_port"), second_payload
    assert first_payload["roar_proxy_port"] != second_payload["roar_proxy_port"], (
        "Expected repeated host-submit jobs to use job-scoped driver proxy ports, "
        f"but both runs reported {first_payload['roar_proxy_port']}."
    )

    first_rows = _proxy_rows_for_key(project_dir, key_suffix=first_payload["key"])
    second_rows = _proxy_rows_for_key(project_dir, key_suffix=second_payload["key"])
    assert first_rows, f"Expected reconstituted proxy rows for first run key {first_payload['key']}"
    assert second_rows, (
        f"Expected reconstituted proxy rows for second run key {second_payload['key']}"
    )
