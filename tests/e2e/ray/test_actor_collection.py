"""E2E: actor-backed Ray log collection works without a shared filesystem."""

from __future__ import annotations

from pathlib import Path

from tests.e2e.ray.conftest import submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


def test_actor_backend_collects_worker_events_without_shared_log_volume(ray_cluster) -> None:
    stdout, stderr, returncode = submit_job_on_head(
        COMPOSE_FILE,
        f"{JOBS_DIR}/basic_file_io.py",
        env={
            "ROAR_WRAP": "1",
            "ROAR_LOG_BACKEND": "actor",
            # Intentionally non-shared location: each container has its own /tmp.
            "ROAR_LOG_DIR": "/tmp/roar-local-logs",
        },
    )
    assert returncode == 0, f"Job failed:\n{stderr}\n{stdout}"

    outputs = _query_roar_db(
        COMPOSE_FILE,
        "SELECT path FROM job_outputs WHERE path LIKE '%output.json'",
    )
    inputs = _query_roar_db(
        COMPOSE_FILE,
        "SELECT path FROM job_inputs WHERE path LIKE '%input.json'",
    )
    assert outputs, "Expected output artifacts when actor backend is enabled."
    assert inputs, "Expected input artifacts when actor backend is enabled."
