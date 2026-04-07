"""Pytest fixtures for the OSMO Docker Compose + KIND harness."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
REPO_ROOT = COMPOSE_FILE.parent.parent.parent.parent.resolve()
HOST_TMP_DIR = REPO_ROOT / ".tmp-osmo-e2e"
HOST_DOWNLOADS_DIR = HOST_TMP_DIR / "downloads"
HOST_PROJECTS_DIR = HOST_TMP_DIR / "projects"
HOST_WHEELS_DIR = HOST_TMP_DIR / "wheels"
CONTAINER_REPO_ROOT = Path("/workspace/roar")
CONTAINER_TMP_DIR = CONTAINER_REPO_ROOT / ".tmp-osmo-e2e"
CONTAINER_DOWNLOADS_DIR = CONTAINER_TMP_DIR / "downloads"
CONTAINER_PROJECTS_DIR = CONTAINER_TMP_DIR / "projects"
CONTAINER_WHEELS_DIR = CONTAINER_TMP_DIR / "wheels"
BASE_URL = "http://quick-start.osmo:38080"
BOOTSTRAP_TIMEOUT_SECONDS = 45 * 60
QUERY_TIMEOUT_SECONDS = 12 * 60
POLL_INTERVAL_SECONDS = 5
PORT_FORWARD_TIMEOUT_SECONDS = 5 * 60
LOCALSTACK_FORWARD_PORT = os.environ.get("OSMO_LOCALSTACK_PORT", "34566")
LOCALSTACK_HOST_OVERRIDE_URL = f"http://127.0.0.1:{LOCALSTACK_FORWARD_PORT}"
HARNESS_STATE_PATH = HOST_TMP_DIR / "harness-state.json"
HARNESS_LOCK_PATH = HOST_TMP_DIR / "harness.lock"
DEFAULT_OSMO_TEST_PYTHON_IMAGE = (
    f"public.ecr.aws/docker/library/python:{sys.version_info.major}.{sys.version_info.minor}-slim"
)
OSMO_TEST_PYTHON_IMAGE = os.environ.get(
    "OSMO_TEST_PYTHON_IMAGE",
    DEFAULT_OSMO_TEST_PYTHON_IMAGE,
)


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


def _compose_env() -> dict[str, str]:
    return {
        **os.environ,
        "OSMO_TEST_PYTHON_IMAGE": OSMO_TEST_PYTHON_IMAGE,
    }


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


def roar_exec(
    args: Sequence[str],
    *,
    cwd: str,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    shell_command = f"cd {shlex.quote(cwd)} && python3 -m roar {shlex.join(list(args))}"
    result = exec_on_service(
        "osmo-tools",
        ["bash", "-lc", shell_command],
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            "Roar command failed:\n"
            f"command: {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )
    return result


def allow_git_safe_directory(path: str | Path) -> None:
    repo_path = str(path)
    result = exec_on_service(
        "osmo-tools",
        [
            "bash",
            "-lc",
            f"cd /tmp && git config --global --add safe.directory {shlex.quote(repo_path)}",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to allow git safe.directory in osmo-tools:\n"
            f"path: {repo_path}\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )


def restore_host_path_ownership(path: str | Path) -> None:
    repo_path = str(path)
    result = exec_on_service(
        "osmo-tools",
        [
            "bash",
            "-lc",
            (f"chown -R {os.getuid()}:{os.getgid()} {shlex.quote(repo_path)}"),
        ],
        timeout=5 * 60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to restore host path ownership from osmo-tools:\n"
            f"path: {repo_path}\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )


def patch_local_osmo_data_override_url(override_url: str) -> None:
    script = f"""
cat <<'EOF' >/root/.config/osmo/config.yaml
auth:
  data:
    s3://osmo:
      access_key: test
      access_key_id: test
      override_url: {override_url}
      region: us-east-1
EOF
"""
    result = exec_on_service(
        "osmo-tools",
        ["bash", "-lc", script],
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to patch local OSMO data override URL.\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )


def publish_runtime_artifact_service(
    *,
    service_name: str,
    host_artifact_path: Path,
    artifact_filename: str,
) -> str:
    image_suffix = hashlib.sha256(host_artifact_path.read_bytes()).hexdigest()[:12]
    image_tag = f"{service_name}:{image_suffix}"
    build_dir = HOST_WHEELS_DIR / f"{service_name}-image"
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(host_artifact_path, build_dir / artifact_filename)
    (build_dir / "Dockerfile").write_text(
        f"""
FROM busybox:1.37.0
COPY {artifact_filename} /srv/{artifact_filename}
CMD ["sh", "-c", "httpd -f -p 8080 -h /srv"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    build_result = run_docker(
        [
            "docker",
            "build",
            "-t",
            image_tag,
            str(build_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20 * 60,
    )
    if build_result.returncode != 0:
        raise RuntimeError(
            "Failed to build runtime artifact image.\n"
            f"stdout:\n{textwrap.indent(build_result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(build_result.stderr, '  ')}"
        )

    load_result = exec_on_service(
        "osmo-tools",
        ["bash", "-lc", f"kind load docker-image --name roar-osmo-e2e {shlex.quote(image_tag)}"],
        timeout=20 * 60,
    )
    if load_result.returncode != 0:
        raise RuntimeError(
            "Failed to load runtime artifact image into kind.\n"
            f"stdout:\n{textwrap.indent(load_result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(load_result.stderr, '  ')}"
        )

    script = f"""
set -euo pipefail
kubectl -n osmo delete deployment {shlex.quote(service_name)} --ignore-not-found >/dev/null
kubectl -n osmo delete service {shlex.quote(service_name)} --ignore-not-found >/dev/null
cat <<'YAML' | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {service_name}
  namespace: osmo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {service_name}
  template:
    metadata:
      labels:
        app: {service_name}
    spec:
      nodeSelector:
        node_group: service
      containers:
        - name: http
          image: {image_tag}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: {service_name}
  namespace: osmo
spec:
  selector:
    app: {service_name}
  ports:
    - port: 80
      targetPort: 8080
YAML
kubectl rollout status deployment/{shlex.quote(service_name)} -n osmo --timeout=5m >/dev/null
"""
    result = exec_on_service(
        "osmo-tools",
        ["bash", "-lc", script],
        timeout=10 * 60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to publish runtime artifact service.\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )
    return f"http://{service_name}.osmo.svc.cluster.local/{artifact_filename}"


def container_repo_path(path: Path) -> Path:
    return CONTAINER_REPO_ROOT / path.resolve().relative_to(REPO_ROOT)


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


def _prepare_host_tmp_dirs() -> None:
    HOST_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    HOST_WHEELS_DIR.mkdir(parents=True, exist_ok=True)


def _read_harness_state() -> dict[str, int | bool]:
    if not HARNESS_STATE_PATH.exists():
        return {"active": False, "refs": 0}
    return json.loads(HARNESS_STATE_PATH.read_text(encoding="utf-8"))


def _write_harness_state(state: Mapping[str, int | bool]) -> None:
    HARNESS_STATE_PATH.write_text(json.dumps(dict(state)), encoding="utf-8")


@contextlib.contextmanager
def _harness_lock() -> None:
    HOST_TMP_DIR.mkdir(parents=True, exist_ok=True)
    with HARNESS_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _teardown_osmo_harness() -> None:
    exec_on_service(
        "osmo-tools",
        ["bash", "tests/backends/osmo/scripts/destroy_osmo.sh"],
        timeout=10 * 60,
    )
    run_docker(
        _compose_args("down", "-v", "--remove-orphans"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10 * 60,
    )


def _setup_osmo_harness() -> None:
    run_docker(
        _compose_args("down", "-v", "--remove-orphans"),
        check=False,
        capture_output=True,
        text=True,
        env=_compose_env(),
        timeout=10 * 60,
    )
    run_docker(
        _compose_args("up", "-d", "--build", "osmo-tools"),
        check=True,
        capture_output=True,
        text=True,
        env=_compose_env(),
        timeout=30 * 60,
    )

    bootstrap_env = {
        key: value
        for key in (
            "OSMO_DOCKERHUB_USERNAME",
            "OSMO_DOCKERHUB_PASSWORD",
            "OSMO_KAI_SCHEDULER_VERSION",
            "OSMO_PRELOAD_DOCKERHUB_IMAGES",
            "OSMO_PRELOAD_PULL_RETRIES",
            "OSMO_QUICK_START_CHART_VERSION",
        )
        if (value := os.environ.get(key))
    }
    bootstrap_env["OSMO_TEST_PYTHON_IMAGE"] = OSMO_TEST_PYTHON_IMAGE
    bootstrap = exec_on_service(
        "osmo-tools",
        ["bash", "tests/backends/osmo/scripts/bootstrap_osmo.sh"],
        env=bootstrap_env or None,
        timeout=BOOTSTRAP_TIMEOUT_SECONDS,
    )
    if bootstrap.returncode != 0:
        raise RuntimeError(
            "OSMO bootstrap failed.\n"
            f"stdout:\n{textwrap.indent(bootstrap.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(bootstrap.stderr, '  ')}"
        )
    patch_local_osmo_data_override_url(LOCALSTACK_HOST_OVERRIDE_URL)


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
    _prepare_host_tmp_dirs()
    HOST_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with _harness_lock():
        state = _read_harness_state()
        if state.get("active"):
            _write_harness_state({"active": True, "refs": int(state.get("refs", 0)) + 1})
        else:
            try:
                _setup_osmo_harness()
            except Exception:
                _teardown_osmo_harness()
                _write_harness_state({"active": False, "refs": 0})
                raise
            _write_harness_state({"active": True, "refs": 1})

    try:
        yield {
            "base_url": BASE_URL,
            "container_downloads_dir": str(CONTAINER_DOWNLOADS_DIR),
            "container_projects_dir": str(CONTAINER_PROJECTS_DIR),
            "container_wheels_dir": str(CONTAINER_WHEELS_DIR),
        }
    finally:
        with _harness_lock():
            state = _read_harness_state()
            refs = max(int(state.get("refs", 0)) - 1, 0)
            if refs > 0:
                _write_harness_state({"active": True, "refs": refs})
            else:
                try:
                    _teardown_osmo_harness()
                finally:
                    _write_harness_state({"active": False, "refs": 0})


@pytest.fixture(scope="session")
def osmo_runtime_wheel(osmo_harness: dict[str, str]) -> dict[str, str]:
    del osmo_harness
    shutil.rmtree(HOST_WHEELS_DIR, ignore_errors=True)
    HOST_WHEELS_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["./scripts/build_wheel_with_bins.sh", str(HOST_WHEELS_DIR)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20 * 60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to build roar wheel for OSMO e2e runtime install.\n"
            f"stdout:\n{textwrap.indent(result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(result.stderr, '  ')}"
        )

    wheels = sorted(HOST_WHEELS_DIR.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"wheel build did not produce a wheel in {HOST_WHEELS_DIR}")
    wheel_path = wheels[-1]
    container_wheel_path = CONTAINER_WHEELS_DIR / wheel_path.name
    install_result = exec_on_service(
        "osmo-tools",
        [
            "bash",
            "-lc",
            (
                "python3 -m pip install --disable-pip-version-check --no-input "
                f"--force-reinstall {shlex.quote(str(container_wheel_path))}"
            ),
        ],
        timeout=15 * 60,
    )
    if install_result.returncode != 0:
        raise RuntimeError(
            "Failed to install roar wheel into osmo-tools.\n"
            f"stdout:\n{textwrap.indent(install_result.stdout, '  ')}\n"
            f"stderr:\n{textwrap.indent(install_result.stderr, '  ')}"
        )
    return {
        "host_path": str(wheel_path),
        "container_path": str(container_wheel_path),
        "cluster_url": publish_runtime_artifact_service(
            service_name="roar-runtime-wheel",
            host_artifact_path=wheel_path,
            artifact_filename=wheel_path.name,
        ),
    }


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

    deadline = time.monotonic() + PORT_FORWARD_TIMEOUT_SECONDS
    process: subprocess.Popen[str] | None = None
    last_log = ""

    try:
        while time.monotonic() < deadline:
            with log_path.open("w", encoding="utf-8") as log_file:
                process = _popen_docker(command, stdout=log_file, stderr=subprocess.STDOUT)

            while time.monotonic() < deadline:
                if process.poll() is not None:
                    last_log = log_path.read_text(encoding="utf-8")
                    process = None
                    time.sleep(2)
                    break

                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    if sock.connect_ex((host, local_port)) == 0:
                        yield {
                            "host": host,
                            "local_port": str(local_port),
                            "task_port": str(task_port),
                            "url": f"http://{host}:{local_port}",
                            "log_path": str(log_path),
                        }
                        return
                time.sleep(1)

        log = log_path.read_text(encoding="utf-8") if log_path.exists() else last_log
        raise RuntimeError(f"Timed out waiting for OSMO port-forward.\nlog:\n{log}")
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
