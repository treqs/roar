"""Ray submit-path contracts for `roar register` step and session targets."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.backends.ray.e2e.conftest import (
    init_host_project,
    make_host_project_dir,
    query_roar_db,
    run_roar_cli_from_host,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(240)]


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


def _current_status_session_hash(project_dir: Path) -> str:
    result = run_roar_cli_from_host(project_dir, "status", timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    match = re.search(r"Session:\s+([a-f0-9]{64})", result.stdout)
    assert match is not None, result.stdout
    return match.group(1)


@pytest.fixture(scope="module")
def register_target_project(ray_cluster: dict[str, str]) -> Path:
    project_dir = make_host_project_dir("register-targets")
    init_host_project(project_dir)
    _run_phase_pipeline(project_dir, ray_cluster)
    return project_dir


def _step_reference_for_command(project_dir: Path, *commands: str) -> str:
    for command in commands:
        rows = query_roar_db(
            project_dir,
            """
            SELECT step_number
            FROM jobs
            WHERE command = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (command,),
        )
        if rows:
            return f"@{int(rows[0]['step_number'])}"

    rows = query_roar_db(
        project_dir,
        """
        SELECT step_number
        FROM jobs
        WHERE step_number IS NOT NULL
        ORDER BY step_number DESC, id DESC
        LIMIT 1
        """,
    )
    assert rows, f"Expected at least one step in local jobs table for commands: {commands!r}"
    return f"@{int(rows[0]['step_number'])}"


def test_register_step_reference_after_ray_submit(register_target_project: Path) -> None:
    step_reference = _step_reference_for_command(
        register_target_project,
        "ray_task:evaluation",
        "ray_task:evaluate_model",
        "ray_task:training",
        "ray_task:extraction",
    )
    result = run_roar_cli_from_host(
        register_target_project, "register", step_reference, "--yes", timeout=60
    )

    assert result.returncode == 0, result.stderr or result.stdout
    session_hash = _parse_session_hash(result.stdout)
    assert len(session_hash) == 64
    assert session_hash in result.stdout


def test_register_session_hash_after_ray_submit(register_target_project: Path) -> None:
    session_hash = _current_status_session_hash(register_target_project)

    result = run_roar_cli_from_host(
        register_target_project,
        "register",
        session_hash,
        "--yes",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert session_hash in result.stdout, result.stdout
