"""Ray lineage contract tests for a simple multi-phase submit pipeline."""

from __future__ import annotations

import json
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

EXPECTED_PHASE_COMMANDS = (
    "ray_task:extraction",
    "ray_task:training",
    "ray_task:evaluation",
)


def _parse_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("script") == "phase_lineage_contract":
            return payload
    raise AssertionError(f"Unable to parse phase-lineage payload from output:\n{stdout}")


def _run_pipeline(project_dir: Path, ray_cluster: dict[str, str]) -> dict[str, object]:
    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "phase_lineage_contract/main.py",
        use_fragment_store=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return _parse_payload(result.stdout)


def _phase_jobs(project_dir: Path) -> list[dict[str, object]]:
    return query_roar_db(
        project_dir,
        """
        SELECT id, step_number, command, script, job_uid
        FROM jobs
        WHERE job_type = 'ray_task'
        ORDER BY step_number, id
        """,
    )


@pytest.fixture(scope="module")
def phase_lineage_contract_run(ray_cluster: dict[str, str]) -> dict[str, object]:
    project_dir = make_host_project_dir("phase-lineage-contract")
    init_host_project(project_dir)
    payload = _run_pipeline(project_dir, ray_cluster)
    return {
        "project_dir": project_dir,
        "payload": payload,
    }


def _jobs_by_command(project_dir: Path) -> dict[str, dict[str, object]]:
    return {str(row["command"]): row for row in _phase_jobs(project_dir)}


def _dag_payload(project_dir: Path) -> dict[str, object]:
    dag_result = run_roar_cli_from_host(project_dir, "dag", "--expanded", "--json", timeout=30)
    assert dag_result.returncode == 0, dag_result.stderr or dag_result.stdout
    dag_payload = json.loads(dag_result.stdout)
    assert isinstance(dag_payload, dict), dag_result.stdout
    return dag_payload


def test_phase_lineage_contract_persists_expected_phase_jobs(
    phase_lineage_contract_run: dict[str, object],
) -> None:
    project_dir = phase_lineage_contract_run["project_dir"]
    assert isinstance(project_dir, Path)
    payload = phase_lineage_contract_run["payload"]
    assert isinstance(payload, dict)

    run_id = str(payload.get("run_id") or "")
    report_key = str(payload.get("report_key") or "")

    assert run_id, payload
    assert report_key.endswith("/reports/final_report.json"), payload

    phase_jobs = _phase_jobs(project_dir)
    jobs_by_command = _jobs_by_command(project_dir)

    missing_commands = [
        command for command in EXPECTED_PHASE_COMMANDS if command not in jobs_by_command
    ]
    assert not missing_commands, (
        "Expected first-class Ray phase jobs in the reconstituted DB, "
        f"missing={missing_commands}, observed={[row['command'] for row in phase_jobs]}"
    )

    extract_step = int(jobs_by_command["ray_task:extract_dataset"]["step_number"])
    train_step = int(jobs_by_command["ray_task:train_model"]["step_number"])
    evaluate_step = int(jobs_by_command["ray_task:evaluate_model"]["step_number"])
    assert extract_step < train_step < evaluate_step, phase_jobs

    report_rows = query_roar_db(
        project_dir,
        """
        SELECT COALESCE(path, first_seen_path) AS path
        FROM artifacts
        WHERE COALESCE(path, first_seen_path) LIKE ?
        """,
        (f"%phase-lineage/{run_id}/reports/final_report.json",),
    )
    assert report_rows, "Expected the final report artifact in the host lineage DB"


def test_phase_lineage_contract_dag_surfaces_dependency_chain(
    phase_lineage_contract_run: dict[str, object],
) -> None:
    project_dir = phase_lineage_contract_run["project_dir"]
    assert isinstance(project_dir, Path)

    jobs_by_command = _jobs_by_command(project_dir)
    dag_payload = _dag_payload(project_dir)
    extract_step = int(jobs_by_command["ray_task:extract_dataset"]["step_number"])
    train_step = int(jobs_by_command["ray_task:train_model"]["step_number"])

    nodes_by_command = {
        str(node.get("command")): node
        for node in dag_payload.get("nodes", [])
        if isinstance(node, dict) and str(node.get("command", "")).startswith("ray_task:")
    }
    missing_nodes = [
        command for command in EXPECTED_PHASE_COMMANDS if command not in nodes_by_command
    ]
    assert not missing_nodes, (
        "Expected `roar dag --expanded --json` to surface the phase Ray jobs, "
        f"missing={missing_nodes}, observed={sorted(nodes_by_command)}"
    )

    assert train_step in nodes_by_command["ray_task:evaluate_model"]["dependencies"], dag_payload
    assert extract_step in nodes_by_command["ray_task:train_model"]["dependencies"], dag_payload


def test_phase_lineage_contract_show_resolves_phase_steps(
    phase_lineage_contract_run: dict[str, object],
) -> None:
    project_dir = phase_lineage_contract_run["project_dir"]
    assert isinstance(project_dir, Path)
    jobs_by_command = _jobs_by_command(project_dir)

    for command in EXPECTED_PHASE_COMMANDS:
        phase_job = jobs_by_command[command]
        step_ref = f"@{int(phase_job['step_number'])}"
        show_result = run_roar_cli_from_host(project_dir, "show", step_ref, timeout=30)
        assert show_result.returncode == 0, show_result.stderr or show_result.stdout
        assert command in show_result.stdout, show_result.stdout
        assert "Job not found" not in show_result.stdout, show_result.stdout
