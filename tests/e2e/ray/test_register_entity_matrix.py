"""Ray host-submit register coverage for publishable entity types."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from roar.services.registration.session import SessionRegistrationService
from tests.e2e.ray.conftest import (
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_cli_from_host,
    run_roar_ray_job_from_host,
)
from tests.live_glaas import test_composite_live as composite_live

managed_glaas_url = composite_live.managed_glaas_url
_api_get = composite_live._api_get
_db_query_rows = composite_live._db_query_rows

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live_glaas,
    pytest.mark.ray_contract,
    pytest.mark.timeout(300),
]


def _cluster_visible_glaas_url(host_url: str) -> str:
    parts = urlsplit(host_url)
    host = parts.hostname or "127.0.0.1"
    if host in {"127.0.0.1", "localhost"}:
        host = "host.docker.internal"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _parse_session_hash(output: str) -> str:
    match = re.search(r"/dag/([a-f0-9]{64})", output)
    if not match:
        raise AssertionError(f"Unable to parse session hash from output:\n{output}")
    return match.group(1)


def _active_session_id(project_dir: Path) -> int:
    rows = query_roar_db(
        project_dir,
        """
        SELECT id
        FROM sessions
        WHERE is_active = 1
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert rows, "Expected an active local roar session"
    return int(rows[0]["id"])


def _step_number_for_command(project_dir: Path, command: str) -> int:
    rows = query_roar_db(
        project_dir,
        """
        SELECT step_number
        FROM jobs
        WHERE command = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (command,),
    )
    assert rows, f"Expected local Ray job command {command!r}"
    return int(rows[0]["step_number"])


def _composite_hash_for_output(project_dir: Path, path: str) -> str:
    rows = query_roar_db(
        project_dir,
        """
        SELECT ah.digest
        FROM job_outputs jo
        JOIN artifacts a ON a.id = jo.artifact_id
        JOIN artifact_hashes ah ON ah.artifact_id = a.id
        WHERE jo.path = ?
          AND ah.algorithm = 'composite-blake3'
        ORDER BY a.first_seen_at DESC
        LIMIT 1
        """,
        (path,),
    )
    assert rows, f"Expected composite artifact hash for {path}"
    return str(rows[0]["digest"])


def test_register_step_reference_after_ray_submit_publishes_ray_remote_files_and_composites(
    ray_cluster: dict[str, str],
    managed_glaas_url: str,
) -> None:
    project_dir = make_host_project_dir("register-entity-matrix")
    init_host_project(project_dir, glaas_url=managed_glaas_url)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "cloud_demo_emulated/main.py",
        use_fragment_store=True,
        extra_env={
            "GLAAS_URL": managed_glaas_url,
            "ROAR_CLUSTER_GLAAS_URL": _cluster_visible_glaas_url(managed_glaas_url),
            "S3_DATA_BUCKET": "test-bucket",
            "S3_MODELS_BUCKET": "output-bucket",
            "S3_RESULTS_BUCKET": "output-bucket",
        },
        timeout=300,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    extraction_step = _step_number_for_command(project_dir, "ray_task:extraction")
    composite_hash = _composite_hash_for_output(project_dir, "s3://test-bucket/sensor_data")

    register_result = run_roar_cli_from_host(
        project_dir,
        "register",
        f"@{extraction_step}",
        "--yes",
        extra_env={
            "AWS_ACCESS_KEY_ID": "minioadmin",
            "AWS_SECRET_ACCESS_KEY": "minioadmin",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": ray_cluster["minio_endpoint"],
        },
        timeout=60,
    )
    assert register_result.returncode == 0, register_result.stderr or register_result.stdout
    session_hash = _parse_session_hash(register_result.stdout)

    expected_session_hash = SessionRegistrationService().compute_session_hash(
        roar_dir=str(project_dir / ".roar"),
        session_id=_active_session_id(project_dir),
    )
    assert session_hash == expected_session_hash, register_result.stdout

    job_rows = _db_query_rows(
        """
        SELECT command, job_type
        FROM jobs
        WHERE session_hash = $1
        ORDER BY command ASC
        """,
        [session_hash],
    )
    ray_task_commands = {
        str(row["command"]) for row in job_rows if str(row.get("job_type") or "") == "ray_task"
    }
    assert "ray_task:extraction" in ray_task_commands, job_rows

    published_paths = _db_query_rows(
        """
        SELECT jo.path, j.command
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE j.session_hash = $1
        ORDER BY j.command ASC, jo.path ASC
        """,
        [session_hash],
    )
    path_set = {str(row["path"]) for row in published_paths}
    assert "s3://test-bucket/sensor_data" in path_set, published_paths
    assert any(path.startswith("s3://test-bucket/sensor_data/shard_") for path in path_set), (
        published_paths
    )

    composite_public = _api_get(managed_glaas_url, f"/api/v1/public/artifacts/{composite_hash}")
    assert composite_public.get("success") is True, composite_public
    assert composite_public["data"]["isComposite"] is True, composite_public

    metadata_rows = _db_query_rows(
        """
        SELECT component_count_total, component_count_stored
        FROM composite_metadata
        WHERE artifact_hash = $1
        """,
        [composite_hash],
    )
    assert len(metadata_rows) == 1, metadata_rows
    assert int(metadata_rows[0]["component_count_total"]) == 25, metadata_rows
    assert int(metadata_rows[0]["component_count_stored"]) == 25, metadata_rows

    component_rows = _db_query_rows(
        """
        SELECT COUNT(*)::int AS component_count
        FROM composite_components
        WHERE composite_hash = $1
        """,
        [composite_hash],
    )
    assert int(component_rows[0]["component_count"]) == 25, component_rows
