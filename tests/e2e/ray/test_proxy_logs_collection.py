"""E2E regression for proxy-log collection after setup-hook S3 I/O."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.ray.conftest import run_docker, submit_job_on_head

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


def _parse_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


@pytest.fixture(autouse=True)
def reset_roar_state(ray_cluster: dict[str, str]) -> None:
    del ray_cluster
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
            "rm -rf /app/.roar && roar init --path /app -n",
        ],
        check=False,
        capture_output=True,
    )
    yield


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_proxy_logs_produce_artifacts(ray_cluster: dict[str, str]) -> None:
    """Proxy-captured S3 logs should become local artifacts after the job completes."""
    del ray_cluster

    stdout, stderr, returncode = submit_job_on_head(
        COMPOSE_FILE,
        f"{JOBS_DIR}/s3_proxy_logs_probe.py",
        env={
            "AWS_ACCESS_KEY_ID": "minioadmin",
            "AWS_ENDPOINT_URL": "http://minio:9000",
            "AWS_SECRET_ACCESS_KEY": "minioadmin",
            "ROAR_RAY_NODE_AGENTS": "1",
            "ROAR_WRAP": "1",
        },
    )
    combined_output = "\n".join(part for part in (stdout, stderr) if part)
    assert returncode == 0, f"Probe runner failed:\n{combined_output}"

    payload = _parse_json_line(stdout)
    assert payload, f"Expected JSON payload in stdout, got:\n{combined_output}"

    status = str(payload.get("status") or "")
    proxy_s3_event_count = int(payload.get("proxy_s3_event_count") or 0)
    db_s3_artifact_count = int(payload.get("db_s3_artifact_count") or 0)

    assert status == "SUCCEEDED", (
        "Expected the submitted Ray job to succeed before validating proxy-log collection.\n"
        f"payload={json.dumps(payload, sort_keys=True)}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    assert proxy_s3_event_count >= 2, (
        "Expected node agents to collect proxy S3 log lines for the submitted workload, "
        "but no real S3 proxy events were observed.\n"
        f"payload={json.dumps(payload, sort_keys=True)}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    assert db_s3_artifact_count > 0, (
        "Expected proxy-captured S3 logs to be materialized into local artifacts after the job, "
        "but the head-node DB still has no matching S3 artifacts.\n"
        "This currently fails because `_collect_ray_io(proxy_logs=...)` drops the collected "
        "node-agent payload via `del proxy_logs` before processing it.\n"
        f"payload={json.dumps(payload, sort_keys=True)}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
