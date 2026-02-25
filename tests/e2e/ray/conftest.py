"""Pytest fixtures for the Docker-based Ray test harness."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

import pytest

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
HEAD_TIMEOUT_SECONDS = 120
WORKERS_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 3


def _import_real_ray():
    blocked_paths = {
        COMPOSE_FILE.parent.resolve(),
        COMPOSE_FILE.parent.parent.resolve(),
    }
    original_sys_path = sys.path[:]
    sys.modules.pop("ray", None)
    try:
        sys.path = [p for p in original_sys_path if Path(p).resolve() not in blocked_paths]
        module = importlib.import_module("ray")
    finally:
        sys.path = original_sys_path

    sys.modules["ray"] = module
    return module


ray = _import_real_ray()


def pytest_configure(config: pytest.Config) -> None:
    config.option.importmode = "importlib"
    config.addinivalue_line("markers", "ray_e2e: Ray end-to-end tests requiring Docker")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.ray_e2e
    for item in items:
        item.add_marker(marker)


def _compose_args(compose_file: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), *args]


def _wait_for_ray_head(compose_file: Path) -> None:
    deadline = time.monotonic() + HEAD_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            _compose_args(compose_file, "exec", "-T", "ray-head", "ray", "status"),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = (result.stderr or result.stdout).strip()
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(f"Timed out waiting for ray-head health: {last_error}")


def _alive_node_count(compose_file: Path) -> int:
    script = (
        "import json, ray; "
        "ray.init(address='auto', ignore_reinit_error=True, logging_level='ERROR'); "
        "alive=[n for n in ray.nodes() if n.get('Alive')]; "
        "print(json.dumps({'alive': len(alive)})); "
        "ray.shutdown()"
    )
    result = subprocess.run(
        _compose_args(compose_file, "exec", "-T", "ray-head", "python", "-c", script),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0

    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "alive" in payload:
            return int(payload["alive"])

    return 0


def _wait_for_workers(compose_file: Path) -> None:
    deadline = time.monotonic() + WORKERS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _alive_node_count(compose_file) >= 3:
            return
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError("Timed out waiting for both Ray workers to register")


@pytest.fixture(scope="session")
def ray_cluster() -> dict[str, str]:
    subprocess.run(
        _compose_args(COMPOSE_FILE, "up", "-d", "--build"),
        check=True,
    )
    try:
        _wait_for_ray_head(COMPOSE_FILE)
        _wait_for_workers(COMPOSE_FILE)
        yield {
            "head_address": "ray://localhost:10001",
            "dashboard_url": "http://localhost:8265",
            "minio_endpoint": "http://localhost:9000",
            "compose_file": str(COMPOSE_FILE),
        }
    finally:
        subprocess.run(
            _compose_args(COMPOSE_FILE, "down", "-v"),
            check=False,
        )


@pytest.fixture(scope="function")
def ray_connection(ray_cluster: dict[str, str]) -> None:
    del ray_cluster
    ray.init(address="ray://localhost:10001", ignore_reinit_error=True)
    try:
        yield
    finally:
        ray.shutdown()


def submit_job_on_head(
    compose_file: str | Path,
    script_path: str,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    compose_path = Path(compose_file)
    command = ["docker", "compose", "-f", str(compose_path), "exec", "-T"]
    if env:
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
    command.extend(["ray-head", "python", script_path])

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result.stdout, result.stderr, result.returncode
