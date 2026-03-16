"""Pytest fixtures for the OSMO Docker Compose + KIND harness."""

from __future__ import annotations

import contextlib
import functools
import json
import shlex
import shutil
import socket
import subprocess
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
REPO_ROOT = COMPOSE_FILE.parent.parent.parent.parent.resolve()
HOST_DOWNLOADS_DIR = REPO_ROOT / ".tmp-osmo-e2e" / "downloads"
CONTAINER_DOWNLOADS_DIR = Path("/workspace/roar/.tmp-osmo-e2e/downloads")
BASE_URL = "http://quick-start.osmo:38080"
BOOTSTRAP_TIMEOUT_SECONDS = 45 * 60
QUERY_TIMEOUT_SECONDS = 12 * 60
POLL_INTERVAL_SECONDS = 5
PORT_FORWARD_TIMEOUT_SECONDS = 5 * 60


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "osmo_e2e: OSMO end-to-end tests requiring a Docker Compose managed KIND harness",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.osmo_e2e
    for item in items:
        item.add_marker(marker)
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(45 * 60))


@functools.lru_cache(maxsize=1)
def _docker_accessible() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def run_docker(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
    command = list(args)
    if _docker_accessible():
        return subprocess.run(command, **kwargs)
    return subprocess.run(["sg", "docker", "-c", shlex.join(command)], **kwargs)


def _compose_args(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *args]


def _compose_exec_command(
    service: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T"]
    if env:
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
    command.append(service)
    command.extend(args)
    return command


def exec_on_service(
    service: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _compose_exec_command(service, args, env=env)
    return run_docker(command, capture_output=True, text=True, check=False, timeout=timeout)


def osmo_exec(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = exec_on_service("osmo-tools", args, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            "OSMO command failed:\n"
            f"command: {' '.join(args)}\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )
    return result


def _remove_downloads_dir() -> None:
    with contextlib.suppress(FileNotFoundError):
        try:
            shutil.rmtree(HOST_DOWNLOADS_DIR)
        except PermissionError:
            run_docker(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{HOST_DOWNLOADS_DIR.parent}:/cleanup",
                    "alpine:3.19",
                    "sh",
                    "-c",
                    "rm -rf /cleanup/downloads",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5 * 60,
            )


def _popen_docker(
    args: Sequence[str],
    *,
    stdout,
    stderr,
) -> subprocess.Popen[str]:
    command = list(args)
    if _docker_accessible():
        return subprocess.Popen(command, stdout=stdout, stderr=stderr, text=True)
    return subprocess.Popen(
        ["sg", "docker", "-c", shlex.join(command)],
        stdout=stdout,
        stderr=stderr,
        text=True,
    )


@pytest.fixture(scope="session")
def osmo_harness() -> dict[str, str]:
    if not shutil.which("docker"):
        pytest.skip("docker is required for OSMO e2e tests")

    _remove_downloads_dir()
    HOST_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    run_docker(
        _compose_args("up", "-d", "--build", "osmo-tools"),
        check=True,
        capture_output=True,
        text=True,
        timeout=30 * 60,
    )

    try:
        bootstrap = exec_on_service(
            "osmo-tools",
            ["bash", "tests/e2e/osmo/scripts/bootstrap_osmo.sh"],
            timeout=BOOTSTRAP_TIMEOUT_SECONDS,
        )
        if bootstrap.returncode != 0:
            raise RuntimeError(
                "OSMO bootstrap failed.\n"
                f"stdout:\n{textwrap.indent(bootstrap.stdout, '  ')}\n"
                f"stderr:\n{textwrap.indent(bootstrap.stderr, '  ')}"
            )
        yield {
            "base_url": BASE_URL,
            "container_downloads_dir": str(CONTAINER_DOWNLOADS_DIR),
        }
    finally:
        exec_on_service(
            "osmo-tools",
            ["bash", "tests/e2e/osmo/scripts/destroy_osmo.sh"],
            timeout=10 * 60,
        )
        run_docker(
            _compose_args("down", "-v"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10 * 60,
        )


def wait_for_workflow_completion(workflow_id: str) -> dict[str, object]:
    return wait_for_workflow_status(workflow_id, lambda status: status == "COMPLETED")


def wait_for_workflow_status(
    workflow_id: str,
    predicate: Callable[[str], bool],
    *,
    timeout_seconds: int = QUERY_TIMEOUT_SECONDS,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, object] | None = None

    while time.monotonic() < deadline:
        result = osmo_exec(
            ["osmo", "workflow", "query", workflow_id, "--format-type", "json"],
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            last_payload = payload
            status = str(payload.get("status", ""))
            if predicate(status):
                return payload
            if status.startswith("FAILED"):
                raise AssertionError(
                    f"Workflow {workflow_id} failed with status {status}.\n"
                    f"payload={json.dumps(payload, indent=2)}"
                )
        time.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(
        f"Timed out waiting for workflow {workflow_id}. "
        f"last_payload={json.dumps(last_payload, indent=2) if last_payload else 'none'}"
    )


@contextlib.contextmanager
def osmo_port_forward(
    workflow_id: str,
    task: str,
    *,
    local_port: int,
    task_port: int,
    host: str = "127.0.0.1",
) -> dict[str, str]:
    HOST_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = HOST_DOWNLOADS_DIR / f"port-forward-{workflow_id}-{task}-{local_port}.log"
    command = _compose_exec_command(
        "osmo-tools",
        [
            "osmo",
            "workflow",
            "port-forward",
            workflow_id,
            task,
            "--host",
            host,
            "--port",
            f"{local_port}:{task_port}",
        ],
    )

    with log_path.open("w+", encoding="utf-8") as log_file:
        process = _popen_docker(command, stdout=log_file, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + PORT_FORWARD_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        "OSMO port-forward exited early.\n"
                        f"log:\n{log_path.read_text(encoding='utf-8')}"
                    )

                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    if sock.connect_ex((host, local_port)) == 0:
                        break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Timed out waiting for OSMO port-forward.\n"
                    f"log:\n{log_path.read_text(encoding='utf-8')}"
                )

            yield {
                "host": host,
                "local_port": str(local_port),
                "task_port": str(task_port),
                "url": f"http://{host}:{local_port}",
                "log_path": str(log_path),
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
