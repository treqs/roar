"""E2E: workers discover the local proxy port from a per-job file."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import run_docker

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
_PORT_FILE_RE = re.compile(r"^/tmp/roar-proxy-([A-Za-z0-9_-]+)\.port$")
def _exec_on_service(
    service: str,
    cmd: str,
    env: dict[str, str] | None = None,
) -> tuple[str, str, int]:
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T"]
    if env:
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
    command.extend([service, "bash", "-lc", cmd])
    result = run_docker(command, capture_output=True, text=True, check=False)
    return result.stdout, result.stderr, result.returncode


def _query_artifact_count() -> int:
    stdout, _, rc = _exec_on_service(
        "ray-head",
        'python3 -c "'
        "import sqlite3; "
        "conn = sqlite3.connect('/app/.roar/roar.db'); "
        "count = conn.execute("
        "\\\"SELECT COUNT(*) FROM artifacts WHERE first_seen_path LIKE 's3://%'\\\""
        ").fetchone()[0]; "
        "print(count); "
        "conn.close()"
        '"',
    )
    if rc != 0:
        return 0
    try:
        return int(stdout.strip())
    except ValueError:
        return 0


def _cleanup_port_files(service: str) -> None:
    _exec_on_service(service, "find /tmp -maxdepth 1 -type f -name 'roar-proxy-*.port' -delete")


def _list_port_files(service: str) -> list[str]:
    stdout, _, rc = _exec_on_service(
        service,
        "find /tmp -maxdepth 1 -type f -name 'roar-proxy-*.port' -print | sort",
    )
    assert rc == 0, f"failed to list proxy port files on {service}:\n{stdout}"
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _read_service_file(service: str, path: str) -> str:
    stdout, stderr, rc = _exec_on_service(service, f"cat {path}")
    assert rc == 0, f"failed to read {path} on {service}:\n{stdout}\n{stderr}"
    return stdout.strip()


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_roar_run_ray_job_uses_worker_port_file_discovery(
    ray_cluster: dict[str, str],
) -> None:
    del ray_cluster

    for service in ("ray-head", "ray-worker-1", "ray-worker-2"):
        _cleanup_port_files(service)

    stdout, stderr, rc = _exec_on_service(
        "ray-head",
        "cd /app && git config --global user.email test@test.com"
        " && git config --global user.name test"
        " && git init -q && git add -A && git commit -q -m init --allow-empty"
        " && rm -rf .roar && roar init --path /app -n",
    )
    assert rc == 0, f"roar init failed:\n{stdout}\n{stderr}"

    env = {
        "AWS_ENDPOINT_URL": "http://minio:9000",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin",
        "ROAR_CLUSTER_PIP_REQ": "skip",
    }
    stdout, stderr, rc = _exec_on_service(
        "ray-head",
        "roar run ray job submit"
        " --address http://127.0.0.1:8265"
        " --working-dir /app"
        " -- python tests/e2e/ray/jobs/s3_workload.py",
        env=env,
    )
    combined = f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert rc == 0, f"roar run ray job submit failed (rc={rc}):\n{combined}"

    worker_job_ids: set[str] = set()
    for service in ("ray-worker-1", "ray-worker-2"):
        port_files = _list_port_files(service)
        assert len(port_files) == 1, (
            f"Expected exactly one proxy port file on {service}, found {port_files}.\n{combined}"
        )

        match = _PORT_FILE_RE.fullmatch(port_files[0])
        assert match is not None, f"Unexpected proxy port file path on {service}: {port_files[0]}"
        worker_job_ids.add(match.group(1))

        port_text = _read_service_file(service, port_files[0])
        assert port_text.isdigit(), f"Expected numeric proxy port in {port_files[0]}, got {port_text!r}"
        port = int(port_text)
        assert 1024 < port <= 65535, f"Proxy port must be unprivileged (>1024), got {port}"

    assert len(worker_job_ids) == 1, f"Expected a shared job id across worker nodes, got {worker_job_ids}"

    artifact_count = _query_artifact_count()
    assert artifact_count > 0, (
        "Expected roar to capture S3 artifacts after file-based proxy discovery, "
        f"but found {artifact_count}.\n{combined}"
    )
