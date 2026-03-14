"""Ray submit-path contracts for `roar register` step and session targets."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from roar.integrations.glaas import GlaasClient
from roar.integrations.glaas.registration.session import SessionRegistrationService
from tests.backends.ray.e2e.conftest import (
    HOST_GLAAS_URL,
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_cli_from_host,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(240)]

EXPECTED_SESSION_COMMANDS = {
    "ray_task:extract_dataset",
    "ray_task:train_model",
    "ray_task:evaluate_model",
}

EXPECTED_STEP_COMMANDS = {
    "ray_task:extraction",
    "ray_task:training",
    "ray_task:evaluation",
}


def _parse_session_hash(output: str) -> str:
    match = re.search(r"/dag/([a-f0-9]{64})", output)
    if not match:
        raise AssertionError(f"Unable to parse session hash from output:\n{output}")
    return match.group(1)


def _run_phase_pipeline(project_dir: Path, ray_cluster: dict[str, str]) -> None:
    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "phase_lineage_contract/main.py",
        use_fragment_store=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr or result.stdout


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


def _session_commands(session_hash: str) -> set[str]:
    client = GlaasClient(HOST_GLAAS_URL)
    session, error = client.get_session(session_hash)
    assert error is None, error
    assert isinstance(session, dict), session
    jobs = session.get("jobs", [])
    assert isinstance(jobs, list), session
    return {
        str(job.get("command"))
        for job in jobs
        if isinstance(job, dict) and str(job.get("command", "")).startswith("ray_task:")
    }


@pytest.fixture(scope="module")
def register_target_project(ray_cluster: dict[str, str]) -> Path:
    project_dir = make_host_project_dir("register-targets")
    init_host_project(project_dir)
    _run_phase_pipeline(project_dir, ray_cluster)
    return project_dir


def test_register_step_reference_after_ray_submit(register_target_project: Path) -> None:
    result = run_roar_cli_from_host(register_target_project, "register", "@4", "--yes", timeout=60)

    assert result.returncode == 0, result.stderr or result.stdout
    session_hash = _parse_session_hash(result.stdout)
    commands = _session_commands(session_hash)
    assert EXPECTED_STEP_COMMANDS.issubset(commands), commands
    assert "ray_task:evaluate_model" in commands, commands


def test_register_session_hash_after_ray_submit(register_target_project: Path) -> None:
    session_id = _active_session_id(register_target_project)
    session_hash = SessionRegistrationService().compute_session_hash(
        roar_dir=str(register_target_project / ".roar"),
        session_id=session_id,
    )

    result = run_roar_cli_from_host(
        register_target_project,
        "register",
        session_hash,
        "--yes",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert session_hash in result.stdout, result.stdout
    commands = _session_commands(session_hash)
    assert EXPECTED_SESSION_COMMANDS.issubset(commands), commands
