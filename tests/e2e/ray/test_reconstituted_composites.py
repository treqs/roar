"""Composite artifact contracts for Ray fragment reconstitution."""

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

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(300)]


def _parse_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("script") == "cloud_demo_emulated":
            return payload
    raise AssertionError(f"Unable to parse cloud-demo-emulated payload from output:\n{stdout}")


@pytest.fixture(scope="module")
def composite_reconstitution_run(ray_cluster: dict[str, str]) -> dict[str, object]:
    project_dir = make_host_project_dir("ray-composites")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "cloud_demo_emulated/main.py",
        use_fragment_store=True,
        extra_env={
            "S3_DATA_BUCKET": "test-bucket",
            "S3_MODELS_BUCKET": "output-bucket",
            "S3_RESULTS_BUCKET": "output-bucket",
        },
        timeout=300,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return {
        "project_dir": project_dir,
        "payload": _parse_payload(result.stdout),
    }


def test_reconstitution_creates_composite_artifact_for_dataset_root(
    composite_reconstitution_run: dict[str, object],
) -> None:
    project_dir = composite_reconstitution_run["project_dir"]
    assert isinstance(project_dir, Path)

    rows = query_roar_db(
        project_dir,
        """
        SELECT a.id, a.kind, a.component_count, jo.path, j.command
        FROM artifacts a
        JOIN job_outputs jo ON jo.artifact_id = a.id
        JOIN jobs j ON j.id = jo.job_id
        WHERE jo.path = 's3://test-bucket/sensor_data'
        ORDER BY j.id DESC
        """,
    )
    assert rows, "Expected reconstitution to add a composite dataset-root artifact output"

    composite_row = rows[0]
    assert composite_row["kind"] == "composite", composite_row
    assert int(composite_row["component_count"]) == 25, composite_row
    assert composite_row["command"] == "ray_task:extraction", composite_row


def test_reconstitution_persists_composite_components_and_membership_index(
    composite_reconstitution_run: dict[str, object],
) -> None:
    project_dir = composite_reconstitution_run["project_dir"]
    assert isinstance(project_dir, Path)

    composite_rows = query_roar_db(
        project_dir,
        """
        SELECT a.id
        FROM artifacts a
        JOIN job_outputs jo ON jo.artifact_id = a.id
        WHERE jo.path = 's3://test-bucket/sensor_data'
        ORDER BY a.first_seen_at DESC
        LIMIT 1
        """,
    )
    assert composite_rows, "Expected a persisted composite artifact for the shard dataset root"
    composite_id = str(composite_rows[0]["id"])

    component_rows = query_roar_db(
        project_dir,
        """
        SELECT relative_path, component_algorithm, component_digest, artifact_id
        FROM composite_artifact_components
        WHERE composite_artifact_id = ?
        ORDER BY ordinal ASC, id ASC
        """,
        (composite_id,),
    )
    assert len(component_rows) == 25, component_rows
    assert component_rows[0]["relative_path"] == "shard_000000.parquet", component_rows[0]
    assert component_rows[-1]["relative_path"] == "shard_000024.parquet", component_rows[-1]
    assert all(str(row["component_algorithm"]).strip() for row in component_rows), component_rows
    assert all(str(row["component_digest"]).strip() for row in component_rows), component_rows
    assert all(str(row["artifact_id"]).strip() for row in component_rows), component_rows

    membership_rows = query_roar_db(
        project_dir,
        """
        SELECT total_components, stored_components, bloom_bits, bloom_hashes, bloom_version
        FROM composite_membership_indexes
        WHERE composite_artifact_id = ?
        """,
        (composite_id,),
    )
    assert membership_rows, "Expected composite membership metadata for the dataset-root artifact"
    membership = membership_rows[0]
    assert int(membership["total_components"]) == 25, membership
    assert int(membership["stored_components"]) == 25, membership
    assert int(membership["bloom_bits"]) > 0, membership
    assert int(membership["bloom_hashes"]) > 0, membership
    assert int(membership["bloom_version"]) == 1, membership
