"""Ray contract: background-thread native writes must stay on the launching Ray task."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.backends.ray.e2e.conftest import (
    decrypt_fragment_batches,
    fetch_fragment_batches,
    init_host_project,
    load_fragment_key,
    make_host_project_dir,
    query_roar_db,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


def _parse_json_line(stdout: str) -> dict[str, dict[str, str]]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            normalized: dict[str, dict[str, str]] = {}
            for key, value in payload.items():
                if isinstance(value, dict):
                    normalized[str(key)] = {str(k): str(v) for k, v in value.items()}
            return normalized
    return {}


def _fragment_entries_for_path(
    fragments: list[dict[str, object]],
    suffix: str,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for fragment in fragments:
        for key in ("reads", "writes"):
            refs = fragment.get(key, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                path = ref.get("path")
                if isinstance(path, str) and path.endswith(suffix):
                    matches.append(
                        {
                            "io_kind": key,
                            "ray_task_id": fragment.get("ray_task_id"),
                            "ray_worker_id": fragment.get("ray_worker_id"),
                            **ref,
                        }
                    )
    return matches


def _output_rows(project_dir: Path, path_like: str) -> list[dict[str, object]]:
    return query_roar_db(
        project_dir,
        """
        SELECT json_extract(j.metadata, '$.ray_task_id') AS ray_task_id,
               COALESCE(a.path, a.first_seen_path) AS path,
               a.capture_method
        FROM jobs j
        JOIN job_outputs jo ON jo.job_id = j.id
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE j.job_type = 'ray_task'
          AND COALESCE(a.path, a.first_seen_path) LIKE ?
        ORDER BY j.id, path
        """,
        (path_like,),
    )


def test_host_submit_reconstitutes_background_thread_native_write_on_launching_task(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-native-background-thread")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "native_background_thread_attribution.py",
        use_fragment_store=True,
        tracer="ptrace",
        extra_env={
            "ROAR_FRAGMENT_IDLE_FLUSH_INTERVAL": "10",
            "ROAR_FRAGMENT_FLUSH_INTERVAL": "10",
        },
    )

    assert result.returncode == 0, (
        f"submit failed (rc={result.returncode})\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )

    payload = _parse_json_line(result.stdout)
    assert payload, f"Expected JSON payload in stdout, got:\n{result.stdout}"
    assert set(payload) == {"launch", "block", "waited"}

    launch = payload["launch"]
    block = payload["block"]
    waited = payload["waited"]

    assert "libroar_tracer_preload.so" in launch.get("ld_preload", "")
    assert launch.get("trace_sock"), launch
    assert launch.get("worker_id"), launch
    assert launch.get("task_id"), launch
    assert launch.get("launch_thread_id"), launch
    assert launch.get("background_thread_id"), launch
    assert launch["background_thread_id"] != launch["launch_thread_id"], payload
    assert waited.get("returncode") == "0", payload

    assert block.get("worker_id") == launch.get("worker_id"), payload
    assert block.get("task_id"), payload
    assert block.get("task_id") != launch.get("task_id"), payload

    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    fragments = decrypt_fragment_batches(batches, key_payload["token"])
    fragment_refs = _fragment_entries_for_path(
        fragments,
        "/artifacts/native_background_thread_output.txt",
    )
    native_refs = [ref for ref in fragment_refs if str(ref.get("capture_method") or "") == "native"]

    assert native_refs, "Expected native fragment refs for the background-thread native output"
    assert {str(ref.get("ray_worker_id") or "") for ref in native_refs} == {launch["worker_id"]}
    assert {str(ref.get("ray_task_id") or "") for ref in native_refs} == {launch["task_id"]}, (
        payload,
        native_refs,
    )

    rows = _output_rows(project_dir, "%/artifacts/native_background_thread_output.txt")
    assert rows, "Expected background-thread native output artifact in the reconstituted roar.db"
    assert {str(row.get("capture_method") or "") for row in rows} == {"native"}
    assert {str(row.get("ray_task_id") or "") for row in rows} == {launch["task_id"]}, (
        payload,
        rows,
    )
