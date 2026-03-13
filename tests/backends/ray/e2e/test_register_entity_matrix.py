"""Ray host-submit register coverage for step references and remote S3 targets."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from roar.services.registration.session import SessionRegistrationService
from tests.backends.ray.e2e.conftest import (
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

EXPECTED_REGISTERED_STEP_COMMANDS = {
    "ray_task:extract_dataset",
    "ray_task:train_model",
    "ray_task:evaluation",
    "ray_task:evaluate_model",
}


def _parse_session_hash(output: str) -> str:
    match = re.search(r"/dag/([a-f0-9]{64})", output)
    if not match:
        raise AssertionError(f"Unable to parse session hash from output:\n{output}")
    return match.group(1)


def _parse_run_info(stdout: str) -> dict[str, str]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_id = payload.get("run_id")
        report_key = payload.get("report_key")
        if isinstance(run_id, str) and isinstance(report_key, str):
            return {"run_id": run_id, "report_key": report_key}
    raise AssertionError(f"Unable to parse run info from output:\n{stdout}")


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


def _artifact_hash_for_output(project_dir: Path, path: str) -> str:
    rows = query_roar_db(
        project_dir,
        """
        SELECT ah.digest
        FROM job_outputs jo
        JOIN artifacts a ON a.id = jo.artifact_id
        JOIN artifact_hashes ah ON ah.artifact_id = a.id
        WHERE jo.path = ?
        ORDER BY
            CASE ah.algorithm
                WHEN 'blake3' THEN 0
                WHEN 'etag' THEN 1
                WHEN 'sha256' THEN 2
                ELSE 99
            END,
            a.first_seen_at DESC
        LIMIT 1
        """,
        (path,),
    )
    assert rows, f"Expected tracked artifact hash for {path}"
    return str(rows[0]["digest"])


@pytest.fixture(scope="module")
def phase_register_project(ray_cluster: dict[str, str], managed_glaas_url: str) -> Path:
    project_dir = make_host_project_dir("register-step-reference")
    init_host_project(project_dir, glaas_url=managed_glaas_url)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "phase_lineage_contract/main.py",
        use_fragment_store=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return project_dir


@pytest.fixture(scope="module")
def s3_register_project(
    ray_cluster: dict[str, str], managed_glaas_url: str
) -> dict[str, str | Path]:
    project_dir = make_host_project_dir("register-s3-target")
    init_host_project(project_dir, glaas_url=managed_glaas_url)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "s3_pipeline.py",
        use_fragment_store=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    run_info = _parse_run_info(result.stdout)
    return {
        "project_dir": project_dir,
        "run_id": run_info["run_id"],
        "report_key": run_info["report_key"],
    }


def test_register_step_reference_after_ray_submit_publishes_phase_session(
    phase_register_project: Path,
) -> None:
    evaluation_step = _step_number_for_command(phase_register_project, "ray_task:evaluation")

    register_result = run_roar_cli_from_host(
        phase_register_project,
        "register",
        f"@{evaluation_step}",
        "--yes",
        timeout=60,
    )
    assert register_result.returncode == 0, register_result.stderr or register_result.stdout
    session_hash = _parse_session_hash(register_result.stdout)

    expected_session_hash = SessionRegistrationService().compute_session_hash(
        roar_dir=str(phase_register_project / ".roar"),
        session_id=_active_session_id(phase_register_project),
    )
    assert session_hash == expected_session_hash, register_result.stdout

    job_rows = _db_query_rows(
        """
        SELECT command
        FROM jobs
        WHERE session_hash = $1
        ORDER BY command ASC
        """,
        [session_hash],
    )
    commands = {str(row["command"]) for row in job_rows}
    assert EXPECTED_REGISTERED_STEP_COMMANDS.issubset(commands), job_rows


def test_register_s3_path_after_ray_submit_publishes_remote_artifact(
    s3_register_project: dict[str, str | Path],
    managed_glaas_url: str,
) -> None:
    project_dir = s3_register_project["project_dir"]
    assert isinstance(project_dir, Path)
    report_key = str(s3_register_project["report_key"])

    artifact_hash = _artifact_hash_for_output(project_dir, report_key)
    register_result = run_roar_cli_from_host(
        project_dir,
        "register",
        report_key,
        "--yes",
        timeout=60,
    )
    assert register_result.returncode == 0, register_result.stderr or register_result.stdout
    session_hash = _parse_session_hash(register_result.stdout)

    expected_session_hash = SessionRegistrationService().compute_session_hash(
        roar_dir=str(project_dir / ".roar"),
        session_id=_active_session_id(project_dir),
    )
    assert session_hash == expected_session_hash, register_result.stdout

    output_rows = _db_query_rows(
        """
        SELECT jo.path
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE j.session_hash = $1
        ORDER BY jo.path ASC
        """,
        [session_hash],
    )
    assert any(str(row["path"]) == report_key for row in output_rows), output_rows

    public_artifact = _api_get(managed_glaas_url, f"/api/v1/public/artifacts/{artifact_hash}")
    assert public_artifact.get("success") is True, public_artifact
    assert public_artifact["data"]["hash"] == artifact_hash, public_artifact
