"""Ray contract: same-process native writes must stay on their originating Ray task."""

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


def test_host_submit_reconstitutes_same_process_native_writes_on_their_originating_task(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-native-thread")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "native_thread_attribution.py",
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
    assert set(payload) == {"fast", "slow"}

    fast = payload["fast"]
    slow = payload["slow"]
    assert "libroar_tracer_preload.so" in fast.get("ld_preload", "")
    assert fast.get("trace_sock"), fast
    assert fast.get("worker_id"), fast
    assert fast.get("task_id"), fast
    assert slow.get("worker_id") == fast.get("worker_id")
    assert slow.get("task_id") and slow.get("task_id") != fast.get("task_id")
    assert fast.get("thread_id") and slow.get("thread_id")
    assert fast["thread_id"] != slow["thread_id"], payload
    assert fast.get("native_thread_id") == fast["thread_id"], payload
    assert slow.get("native_thread_id") == slow["thread_id"], payload
    assert fast.get("pre_write_bound_task_id") == fast["task_id"], payload
    assert slow.get("pre_write_bound_task_id") == slow["task_id"], payload

    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    fragments = decrypt_fragment_batches(batches, key_payload["token"])

    expectations = {
        "fast": ("/artifacts/native_thread_fast.txt", fast["task_id"]),
        "slow": ("/artifacts/native_thread_slow.txt", slow["task_id"]),
    }
    for label, (suffix, expected_task_id) in expectations.items():
        fragment_refs = _fragment_entries_for_path(fragments, suffix)
        native_refs = [
            ref for ref in fragment_refs if str(ref.get("capture_method") or "") == "native"
        ]

        assert native_refs, f"Expected native fragment refs for {label} output"
        assert {str(ref.get("ray_worker_id") or "") for ref in native_refs} == {fast["worker_id"]}
        assert {str(ref.get("ray_task_id") or "") for ref in native_refs} == {expected_task_id}, (
            native_refs
        )

        rows = _output_rows(project_dir, f"%{suffix}")
        assert rows, f"Expected reconstituted roar.db rows for {label} output"
        assert {str(row.get("capture_method") or "") for row in rows} == {"native"}
        assert {str(row.get("ray_task_id") or "") for row in rows} == {expected_task_id}, rows
