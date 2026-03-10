"""User-facing Ray contract tests for `roar run ray job submit ...`."""

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


def _assert_submit_ok(result) -> None:
    assert result.returncode == 0, (
        f"submit failed (rc={result.returncode})\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )


def _parse_last_json(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"Unable to parse JSON payload from stdout:\n{stdout}")


def _artifact_rows(project_dir: Path, path_like: str) -> list[dict[str, object]]:
    return query_roar_db(
        project_dir,
        """
        SELECT id,
               COALESCE(path, first_seen_path) AS path,
               capture_method,
               metadata
        FROM artifacts
        WHERE COALESCE(path, first_seen_path) LIKE ?
        ORDER BY id
        """,
        (path_like,),
    )


def _output_rows(project_dir: Path, path_like: str) -> list[dict[str, object]]:
    return query_roar_db(
        project_dir,
        """
        SELECT j.id AS job_id,
               json_extract(j.metadata, '$.ray_task_id') AS ray_task_id,
               json_extract(j.metadata, '$.ray_node_id') AS ray_node_id,
               COALESCE(a.path, a.first_seen_path) AS path
        FROM jobs j
        JOIN job_outputs jo ON jo.job_id = j.id
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE j.job_type = 'ray_task'
          AND COALESCE(a.path, a.first_seen_path) LIKE ?
        ORDER BY j.id, path
        """,
        (path_like,),
    )


def test_local_file_io_reconstitutes_into_host_roar_db(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-contract")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "basic_file_io.py",
        use_fragment_store=True,
    )

    _assert_submit_ok(result)
    inputs = query_roar_db(
        project_dir,
        "SELECT path FROM job_inputs WHERE path LIKE ?",
        ("%/artifacts/basic_file_io/%",),
    )
    outputs = query_roar_db(
        project_dir,
        "SELECT path FROM job_outputs WHERE path LIKE ?",
        ("%/artifacts/basic_file_io/%",),
    )
    jobs = query_roar_db(
        project_dir,
        """
        SELECT job_uid, parent_job_uid, json_extract(metadata, '$.ray_task_id') AS ray_task_id
        FROM jobs
        WHERE job_type = 'ray_task'
        """,
    )

    assert inputs
    assert outputs
    assert jobs
    assert all(row["ray_task_id"] for row in jobs)
    assert any(str(row["path"]).endswith("/artifacts/basic_file_io/input.json") for row in inputs)
    assert any(str(row["path"]).endswith("/artifacts/basic_file_io/output.json") for row in outputs)


def test_s3_proxy_routing_captures_worker_inputs_and_outputs(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-contract")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "s3_io.py",
        use_fragment_store=True,
    )

    _assert_submit_ok(result)
    artifacts = _artifact_rows(project_dir, "s3://%")
    inputs = query_roar_db(
        project_dir,
        "SELECT path FROM job_inputs WHERE path LIKE 's3://%' ORDER BY path",
    )
    outputs = query_roar_db(
        project_dir,
        "SELECT path FROM job_outputs WHERE path LIKE 's3://%' ORDER BY path",
    )

    assert artifacts
    assert inputs
    assert outputs
    assert any(row["capture_method"] == "proxy" for row in artifacts)


def test_artifacts_are_attributed_to_distinct_ray_tasks(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-contract")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "attributed_file_io.py",
        use_fragment_store=True,
    )

    _assert_submit_ok(result)
    payload = _parse_last_json(result.stdout)
    writes = payload.get("writes", [])
    assert isinstance(writes, list) and len(writes) == 6

    outputs = _output_rows(project_dir, "%/artifacts/attributed/%")
    task_ids = {row["ray_task_id"] for row in outputs if row["ray_task_id"]}
    output_paths = {row["path"] for row in outputs if row["path"]}

    assert len(task_ids) == 6
    assert len(output_paths) == 6


def test_multi_node_lineage_merges_jobs_from_multiple_nodes(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-contract")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "s3_multi_node_affinity.py",
        use_fragment_store=True,
    )

    _assert_submit_ok(result)
    payload = _parse_last_json(result.stdout)
    results = payload.get("results", [])
    assert isinstance(results, list) and results

    runtime_node_ids = {str(item.get("node_id") or "") for item in results if isinstance(item, dict)}
    runtime_node_ids.discard("")
    assert len(runtime_node_ids) >= 2

    run_id = str(payload["run_id"])
    db_rows = query_roar_db(
        project_dir,
        """
        SELECT DISTINCT json_extract(j.metadata, '$.ray_node_id') AS ray_node_id
        FROM jobs j
        JOIN job_outputs jo ON jo.job_id = j.id
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE j.job_type = 'ray_task'
          AND COALESCE(a.path, a.first_seen_path) LIKE ?
        """,
        (f"%multi-node-affinity/{run_id}/%",),
    )
    db_node_ids = {str(row["ray_node_id"] or "") for row in db_rows}
    db_node_ids.discard("")

    assert len(db_node_ids) >= 2


def test_fragments_capture_tmp_paths_but_reconstitution_filters_them_by_default(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("ray-contract")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "tmp_filter_probe.py",
        use_fragment_store=True,
    )

    _assert_submit_ok(result)
    payload = _parse_last_json(result.stdout)
    workspace_path = str(payload["workspace_path"])
    tmp_path_str = str(payload["tmp_path"])

    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    fragments = decrypt_fragment_batches(batches, key_payload["token"])

    captured_paths = set()
    for fragment in fragments:
        for key in ("reads", "writes"):
            refs = fragment.get(key, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                    captured_paths.add(ref["path"])

    assert workspace_path in captured_paths
    assert tmp_path_str in captured_paths

    kept_rows = _artifact_rows(project_dir, f"%{Path(workspace_path).name}")
    filtered_rows = _artifact_rows(project_dir, f"%{Path(tmp_path_str).name}")

    assert kept_rows
    assert not filtered_rows


def test_contract_workloads_remain_roar_unaware() -> None:
    jobs_dir = Path(__file__).resolve().parent / "jobs"
    workload_names = [
        "basic_file_io.py",
        "attributed_file_io.py",
        "s3_io.py",
        "s3_pipeline.py",
        "tmp_filter_probe.py",
    ]

    for name in workload_names:
        text = (jobs_dir / name).read_text(encoding="utf-8")
        assert "import roar" not in text
        assert "from roar" not in text
        assert "ROAR_" not in text
