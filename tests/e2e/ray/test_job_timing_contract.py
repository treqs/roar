"""Timing contracts for Ray lineage jobs on the real submit path."""

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

PHASE_COMMAND = "ray_task:timing_phase"
TASK_COMMAND = "ray_task:timed_write"


def _parse_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("script") == "timing_contract":
            return payload
    raise AssertionError(f"Unable to parse timing payload from output:\n{stdout}")


@pytest.fixture(scope="module")
def timing_contract_run(ray_cluster: dict[str, str]) -> dict[str, object]:
    project_dir = make_host_project_dir("ray-job-timing")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "timing_contract/main.py",
        use_fragment_store=True,
        extra_env={"S3_RESULTS_BUCKET": "output-bucket"},
        timeout=300,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return {
        "project_dir": project_dir,
        "payload": _parse_payload(result.stdout),
    }


def _job_row(project_dir: Path, command: str) -> dict[str, object]:
    rows = query_roar_db(
        project_dir,
        """
        SELECT id, timestamp, duration_seconds, command, script, job_type
        FROM jobs
        WHERE command = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (command,),
    )
    assert rows, f"Expected a persisted Ray job for {command}"
    return rows[0]


def test_phase_job_timestamp_matches_phase_start_time(
    timing_contract_run: dict[str, object],
) -> None:
    project_dir = timing_contract_run["project_dir"]
    assert isinstance(project_dir, Path)
    payload = timing_contract_run["payload"]
    assert isinstance(payload, dict)

    phase_job = _job_row(project_dir, PHASE_COMMAND)
    phase_started_at = float(payload["phase_started_at"])
    phase_ended_at = float(payload["phase_ended_at"])

    assert abs(float(phase_job["timestamp"]) - phase_started_at) < 0.5, (
        "Expected the persisted phase job timestamp to reflect when the phase started, "
        f"job={phase_job}, phase_started_at={phase_started_at}, phase_ended_at={phase_ended_at}"
    )


def test_task_job_timestamp_matches_task_start_time(
    timing_contract_run: dict[str, object],
) -> None:
    project_dir = timing_contract_run["project_dir"]
    assert isinstance(project_dir, Path)
    payload = timing_contract_run["payload"]
    assert isinstance(payload, dict)

    task_job = _job_row(project_dir, TASK_COMMAND)
    task_started_at = float(payload["task_started_at"])

    assert abs(float(task_job["timestamp"]) - task_started_at) < 0.5, (
        "Expected the persisted task job timestamp to reflect task start time, "
        f"job={task_job}, task_started_at={task_started_at}"
    )


def test_task_job_duration_tracks_full_task_wall_time(
    timing_contract_run: dict[str, object],
) -> None:
    project_dir = timing_contract_run["project_dir"]
    assert isinstance(project_dir, Path)
    payload = timing_contract_run["payload"]
    assert isinstance(payload, dict)

    task_job = _job_row(project_dir, TASK_COMMAND)
    task_expected_duration_seconds = float(payload["task_expected_duration_seconds"])

    assert float(task_job["duration_seconds"]) >= task_expected_duration_seconds - 0.2, (
        "Expected the persisted task duration to cover the full task wall time, "
        f"job={task_job}, expected_duration={task_expected_duration_seconds}"
    )
