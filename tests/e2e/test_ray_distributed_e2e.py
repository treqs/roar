"""E2E validation for distributed Ray execution via docker-compose."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from roar.db.context import create_database_context

pytestmark = [pytest.mark.e2e]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    existing_pythonpath = run_env.get("PYTHONPATH")
    if existing_pythonpath:
        run_env["PYTHONPATH"] = f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}"
    else:
        run_env["PYTHONPATH"] = str(PROJECT_ROOT)

    result = subprocess.run(
        args,
        cwd=cwd,
        env=run_env,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_port(env_name: str, fallback: int) -> int:
    configured = os.environ.get(env_name)
    if configured:
        try:
            value = int(configured)
            if 1 <= value <= 65535:
                return value
        except ValueError:
            pass

    try:
        return _free_port()
    except OSError:
        # Some restricted sandboxes disallow local socket bind; use a stable fallback.
        return fallback


def _wait_for_cluster(compose_file: Path, env: dict[str, str], cwd: Path, timeout: float) -> None:
    deadline = time.time() + timeout
    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "ray-head",
        "ray",
        "status",
        "--address=127.0.0.1:6379",
    ]
    while time.time() < deadline:
        result = _run(cmd, cwd=cwd, env=env, check=False)
        if result.returncode == 0:
            return
        time.sleep(2.0)
    raise TimeoutError("Timed out waiting for Ray cluster readiness")


def _blake3_digest(item: dict) -> str | None:
    for hash_item in item.get("hashes", []):
        if hash_item.get("algorithm") == "blake3":
            return hash_item.get("digest")
    return None


def _docker_daemon_available() -> bool:
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.timeout(300)
def test_run_ray_backend_records_distributed_lineage(tmp_path: Path) -> None:
    if os.environ.get("ROAR_RUN_DOCKER_E2E") != "1":
        pytest.skip("Set ROAR_RUN_DOCKER_E2E=1 to run docker-compose E2E tests")
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for Ray docker-compose E2E test")
    if not _docker_daemon_available():
        pytest.skip("Docker daemon is not accessible for Ray docker-compose E2E test")
    if shutil.which("git") is None:
        pytest.skip("git is required for this E2E workflow")
    try:
        import ray  # noqa: F401
    except Exception:
        pytest.skip("Ray package is not installed in test environment")

    repo = tmp_path / "repo"
    repo.mkdir()
    compose_file = Path(__file__).parent / "ray_cluster" / "docker-compose.yml"

    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test User"], cwd=repo)
    (repo / "README.md").write_text("ray e2e\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "initial"], cwd=repo)

    _run([sys.executable, "-m", "roar", "init", "-y"], cwd=repo)
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "commit", "-m", "init roar"], cwd=repo)

    workflow = repo / "workflow.py"
    workflow.write_text(
        """
import os

import ray

from roar.ray import record_input_ref, record_output_ref, traced_remote

runtime_env = {}
source_dir = os.environ.get("ROAR_SOURCE_DIR")
if source_dir:
    runtime_env["working_dir"] = source_dir

ray.init(
    address=os.environ.get("ROAR_RAY_ADDRESS", "auto"),
    namespace=os.environ.get("ROAR_RAY_NAMESPACE", "roar"),
    log_to_driver=False,
    runtime_env=runtime_env or None,
)


@traced_remote
def load_data():
    record_output_ref("ref:features")
    return [1, 2, 3, 4]


@traced_remote
def train(features):
    record_input_ref("ref:features")
    record_output_ref("ref:model")
    return sum(features)


features_ref = load_data.remote()
model_ref = train.remote(features_ref)
ray.get(model_ref)
ray.shutdown()
""".strip()
        + "\n"
    )
    _run(["git", "add", "workflow.py"], cwd=repo)
    _run(["git", "commit", "-m", "add workflow"], cwd=repo)

    gcs_port = _resolve_port("ROAR_RAY_HEAD_PORT", 16379)
    client_port = _resolve_port("ROAR_RAY_CLIENT_PORT", 17001)
    project_name = f"roar-ray-e2e-{os.getpid()}-{int(time.time())}"
    compose_env = dict(os.environ)
    compose_env["COMPOSE_PROJECT_NAME"] = project_name
    compose_env["ROAR_RAY_HEAD_PORT"] = str(gcs_port)
    compose_env["ROAR_RAY_CLIENT_PORT"] = str(client_port)

    up_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "up",
        "-d",
        "--scale",
        "ray-worker=2",
    ]
    down_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "down",
        "-v",
        "--remove-orphans",
    ]

    _run(up_cmd, cwd=repo, env=compose_env)
    try:
        _wait_for_cluster(compose_file, compose_env, repo, timeout=120.0)

        run_cmd = [
            sys.executable,
            "-m",
            "roar",
            "run",
            "--backend",
            "ray",
            "--ray-address",
            f"ray://127.0.0.1:{client_port}",
            "--ray-namespace",
            "roar",
            sys.executable,
            "workflow.py",
        ]
        run_result = _run(
            run_cmd,
            cwd=repo,
            env={"ROAR_SOURCE_DIR": str(PROJECT_ROOT)},
            check=False,
        )
        assert run_result.returncode == 0, (
            f"stdout:\n{run_result.stdout}\n\nstderr:\n{run_result.stderr}"
        )

        with create_database_context(repo / ".roar") as db_ctx:
            jobs = db_ctx.jobs.get_recent(20)
            assert any(str(job["command"]).startswith("ray::") and "load_data" in str(job["command"]) for job in jobs)
            assert any(str(job["command"]).startswith("ray::") and "train" in str(job["command"]) for job in jobs)

            model_artifact = db_ctx.artifacts.get_by_path("ray://object/ref:model")
            assert model_artifact is not None
            model_hash = _blake3_digest(model_artifact)
            assert model_hash is not None

            lineage_jobs = db_ctx.lineage.get_lineage_jobs([model_hash])
            lineage_commands = {job["command"] for job in lineage_jobs}
            assert any(str(command).startswith("ray::") and "load_data" in str(command) for command in lineage_commands)
            assert any(str(command).startswith("ray::") and "train" in str(command) for command in lineage_commands)
    finally:
        _run(down_cmd, cwd=repo, env=compose_env, check=False)
