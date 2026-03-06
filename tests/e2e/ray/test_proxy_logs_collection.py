"""E2E: roar run ray job submit captures S3 proxy logs as artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.ray.conftest import run_docker

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"


def _exec_on_head(cmd: str, env: dict[str, str] | None = None) -> tuple[str, str, int]:
    """Run a shell command on the ray-head container."""
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T"]
    if env:
        for k, v in env.items():
            command.extend(["-e", f"{k}={v}"])
    command.extend(["ray-head", "bash", "-c", cmd])
    result = run_docker(command, capture_output=True, text=True, check=False)
    return result.stdout, result.stderr, result.returncode


def _query_artifact_count() -> int:
    """Query roar.db on the head node for proxy-captured S3 artifacts."""
    stdout, _, rc = _exec_on_head(
        'python3 -c "'
        "import sqlite3, sys; "
        "conn = sqlite3.connect('/app/.roar/roar.db'); "
        "count = conn.execute("
        "\\\"SELECT COUNT(*) FROM artifacts WHERE first_seen_path LIKE 's3://%'\\\""
        ").fetchone()[0]; "
        "print(count); "
        "conn.close()"
        '"'
    )
    if rc != 0:
        return 0
    try:
        return int(stdout.strip())
    except ValueError:
        return 0


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_roar_run_ray_job_captures_s3_artifacts(ray_cluster: dict[str, str]) -> None:
    """roar run ray job submit should capture S3 I/O via proxy into local artifacts.

    FAILS until the `del proxy_logs` bug in _collect_ray_io is fixed.
    """
    del ray_cluster

    # Init git + roar project (roar requires a git repo)
    stdout, stderr, rc = _exec_on_head(
        "cd /app && git config --global user.email test@test.com"
        " && git config --global user.name test"
        " && git init -q && git add -A && git commit -q -m init --allow-empty"
        " && rm -rf .roar && roar init --path /app -n"
    )
    assert rc == 0, f"roar init failed:\n{stdout}\n{stderr}"

    # Run the job via the real production path
    env = {
        "AWS_ENDPOINT_URL": "http://minio:9000",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin",
    }
    stdout, stderr, rc = _exec_on_head(
        "roar run ray job submit"
        " --address http://127.0.0.1:8265"
        " --working-dir /app"
        " -- python tests/e2e/ray/jobs/s3_workload.py",
        env=env,
    )
    combined = f"stdout:\n{stdout}\nstderr:\n{stderr}"

    # Job should succeed
    assert rc == 0, f"roar run ray job submit failed (rc={rc}):\n{combined}"

    # After roar run completes, proxy logs should have been collected
    # and reconstituted into local artifacts
    count = _query_artifact_count()
    assert count > 0, (
        "Expected roar to capture S3 artifacts via proxy after "
        "`roar run ray job submit`, but found 0.\n"
        "This fails because `_collect_ray_io(proxy_logs=...)` does "
        "`del proxy_logs` before processing.\n"
        f"artifact_count={count}\n{combined}"
    )
