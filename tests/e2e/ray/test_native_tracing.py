"""TDD: Ray workers get native preload tracing via runtime_env wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import run_docker, submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


@pytest.fixture(autouse=True)
def reset_roar_state(ray_cluster):
    """Reset roar state on the head node before each test."""
    run_docker(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "ray-head",
            "bash",
            "-c",
            "rm -rf /app/.roar /shared/.roar-logs && roar init --path /app -n",
        ],
        check=False,
        capture_output=True,
    )
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
        stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/native_tracing.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}\n{stdout}"

        payload = _parse_json_line(stdout)
        assert payload, f"Expected JSON payload in stdout, got:\n{stdout}"
        assert "libroar_tracer_preload.so" in payload.get("ld_preload", "")

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT first_seen_path FROM artifacts WHERE first_seen_path LIKE '%native_tracing_output.txt'",
        )
        assert rows, "Expected native tracing output artifact to be captured in roar.db"
