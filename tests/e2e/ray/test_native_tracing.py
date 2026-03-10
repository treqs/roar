"""Ray contract: host submit activates worker native tracing and reconstitutes it."""

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


def _artifact_rows(project_dir: Path, path_like: str) -> list[dict[str, object]]:
    return query_roar_db(
        project_dir,
        """
        SELECT COALESCE(path, first_seen_path) AS path,
               capture_method
        FROM artifacts
        WHERE COALESCE(path, first_seen_path) LIKE ?
        ORDER BY id
        """,
        (path_like,),
    )


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


def _native_entries_for_worker(
    fragments: list[dict[str, object]],
    worker_id: str,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for fragment in fragments:
        if str(fragment.get("ray_worker_id") or "") != worker_id:
            continue
        for key in ("reads", "writes"):
            refs = fragment.get(key, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                if str(ref.get("capture_method") or "") != "native":
                    continue
                matches.append(
                    {
                        "io_kind": key,
                        "ray_task_id": fragment.get("ray_task_id"),
                        "ray_worker_id": fragment.get("ray_worker_id"),
                        **ref,
                    }
                )
    return matches


def test_host_submit_reconstitutes_native_worker_lineage(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-native")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "native_tracing.py",
        use_fragment_store=True,
        tracer="ptrace",
    )

    assert result.returncode == 0, (
        f"submit failed (rc={result.returncode})\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )

    payload = _parse_json_line(result.stdout)
    assert payload, f"Expected JSON payload in stdout, got:\n{result.stdout}"
    assert "libroar_tracer_preload.so" in payload.get("ld_preload", "")
    assert payload.get("trace_sock"), payload

    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    fragments = decrypt_fragment_batches(batches, key_payload["token"])
    fragment_refs = _fragment_entries_for_path(fragments, "/artifacts/native_tracing_output.txt")
    native_worker_refs = _native_entries_for_worker(fragments, payload.get("worker_id", ""))

    assert fragment_refs, "Expected fragment payloads for the worker output artifact"
    assert any(str(ref.get("ray_worker_id")) == payload.get("worker_id", "") for ref in fragment_refs), (
        "Expected the output artifact fragments to belong to the worker that reported preload activation"
    )
    assert native_worker_refs, (
        "Expected at least one native fragment entry from the same Ray worker that reported "
        "preload activation"
    )

    rows = _artifact_rows(project_dir, "%/artifacts/native_tracing_output.txt")
    assert rows, "Expected worker output artifact to be reconstituted into the host roar.db"
    assert "[roar] lineage reconstituted:" in f"{result.stdout}\n{result.stderr}"
