"""TDD: Ray workers get native preload tracing via runtime_env wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import (
    query_roar_db_on_head,
    reset_roar_project_on_head,
    run_roar_ray_job_on_head,
)

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"
pytestmark = [pytest.mark.e2e, pytest.mark.ray_diagnostic, pytest.mark.timeout(180)]


@pytest.fixture(autouse=True)
def reset_roar_state(ray_cluster):
    """Reset roar state on the head node before each test."""
    del ray_cluster
    reset_roar_project_on_head(COMPOSE_FILE)
    yield


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


class TestNativeTracing:
    def test_worker_ld_preload_and_artifact_capture(self, ray_cluster):
        stdout, stderr, returncode = run_roar_ray_job_on_head(
            f"{JOBS_DIR}/native_tracing.py",
            compose_file=COMPOSE_FILE,
            use_fragment_store=True,
        )
        assert returncode == 0, f"Job failed:\n{stderr}\n{stdout}"

        payload = _parse_json_line(stdout)
        assert payload, f"Expected JSON payload in stdout, got:\n{stdout}"
        assert "libroar_tracer_preload.so" in payload.get("ld_preload", "")

        rows = query_roar_db_on_head(
            "SELECT first_seen_path FROM artifacts WHERE first_seen_path LIKE '%native_tracing_output.txt'",
        )
        assert rows, "Expected native tracing output artifact to be captured in roar.db"
