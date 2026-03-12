"""Pytest fixtures for the Docker-based Ray test harness."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import functools
import importlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
REPO_ROOT = COMPOSE_FILE.parent.parent.parent.parent.resolve()
ROAR_BIN = REPO_ROOT / ".venv" / "bin" / "roar"
PYTHON_ENV_ROAR_BIN = Path(sys.executable).with_name("roar")
HOST_JOBS_DIR = COMPOSE_FILE.parent / "jobs"
HOST_PROJECTS_DIR = REPO_ROOT.parent / ".tmp-ray-e2e"
HOST_GLAAS_URL = "http://localhost:3001"
CLUSTER_GLAAS_URL = "http://host.docker.internal:3001"
HEAD_PROJECT_DIR = "/app"
JOBS_DIR = f"{HEAD_PROJECT_DIR}/tests/e2e/ray/jobs"
FRAGMENT_STORE_URL = CLUSTER_GLAAS_URL
HEAD_TIMEOUT_SECONDS = 120
WORKERS_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 3
RAY_CLUSTER_LOCK_FILE = Path(tempfile.gettempdir()) / "roar-ray-e2e.lock"


def _is_compiled_extension(module: object) -> bool:
    module_file = getattr(module, "__file__", "")
    return isinstance(module_file, str) and module_file.endswith((".so", ".pyd", ".dll"))


def _import_real_ray():
    blocked_paths = {
        COMPOSE_FILE.parent.resolve(),
        COMPOSE_FILE.parent.parent.resolve(),
    }
    preserved = {
        key: value
        for key, value in sys.modules.items()
        if key.startswith("ray.") and _is_compiled_extension(value)
    }
    original_sys_path = sys.path[:]
    ray_keys = [
        key for key in sys.modules if (key == "ray" or key.startswith("ray.")) and key != __name__
    ]
    for key in ray_keys:
        sys.modules.pop(key, None)
    try:
        sys.path = [p for p in original_sys_path if Path(p).resolve() not in blocked_paths]
        module = importlib.import_module("ray")
        importlib.import_module("ray._raylet")
    finally:
        sys.path = original_sys_path

    for key, value in preserved.items():
        sys.modules.setdefault(key, value)
    sys.modules["ray"] = module
    return module


ray = None


def _get_ray():
    global ray
    if ray is None:
        ray = _import_real_ray()
    return ray


def pytest_configure(config: pytest.Config) -> None:
    # Ensure subprocess calls in tests can find tools installed in this repo's
    # virtualenv (for example the `ray` CLI used by infra health checks).
    current_path = os.environ.get("PATH", "")
    path_entries = current_path.split(":") if current_path else []
    for bin_dir in (PYTHON_ENV_ROAR_BIN.parent, (REPO_ROOT / ".venv" / "bin")):
        if not bin_dir.exists():
            continue
        bin_dir_text = str(bin_dir)
        if bin_dir_text not in path_entries:
            os.environ["PATH"] = f"{bin_dir_text}:{current_path}" if current_path else bin_dir_text
            break

    config.option.importmode = "importlib"
    with contextlib.suppress(
        ModuleNotFoundError
    ):  # Ray not installed; e2e tests require a live Docker cluster
        _get_ray()
    config.addinivalue_line("markers", "ray_e2e: Ray end-to-end tests requiring Docker")
    config.addinivalue_line(
        "markers",
        "ray_contract: User-facing Ray contract tests using `roar run ray job submit ...`",
    )
    config.addinivalue_line(
        "markers",
        "ray_diagnostic: Diagnostic Ray tests that inspect internal runtime details",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.ray_e2e
    for item in items:
        item.add_marker(marker)
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(180))


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


def run_docker(args: Sequence[str], **kwargs):
    command = list(args)
    if _docker_accessible():
        return subprocess.run(command, **kwargs)
    return subprocess.run(["sg", "docker", "-c", shlex.join(command)], **kwargs)


def _compose_args(compose_file: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), *args]


@contextlib.contextmanager
def _exclusive_ray_cluster_lock():
    """Serialize the Docker-backed Ray cluster across xdist workers.

    The compose stack uses fixed network and container names, so parallel
    session fixtures on different workers will race `docker compose up/down`.
    Holding the lock for the fixture lifetime makes Ray e2e effectively serial,
    which is acceptable for this harness.
    """

    RAY_CLUSTER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RAY_CLUSTER_LOCK_FILE.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _wait_for_ray_head(compose_file: Path) -> None:
    deadline = time.monotonic() + HEAD_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = run_docker(
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
    result = run_docker(
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


def _ensure_roar_db(compose_file: Path) -> None:
    """
    Ensure roar is initialised on the head node before tests run.
    Idempotent: harmless if .roar already exists.
    """
    run_docker(
        _compose_args(
            compose_file,
            "exec",
            "-T",
            "ray-head",
            "bash",
            "-c",
            "test -f /app/.roar/roar.db || (rm -rf /app/.roar && roar init --path /app -n)",
        ),
        check=False,
        capture_output=True,
    )


def exec_on_service(
    service: str,
    args: Sequence[str],
    *,
    compose_file: str | Path = COMPOSE_FILE,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    compose_path = Path(compose_file)
    command = ["docker", "compose", "-f", str(compose_path), "exec", "-T"]
    if env:
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
    command.append(service)
    command.extend(args)
    return run_docker(command, capture_output=True, text=True, check=False, timeout=timeout)


def _roar_bin() -> str:
    for candidate in (ROAR_BIN, PYTHON_ENV_ROAR_BIN):
        if candidate.exists():
            return str(candidate)
    return "roar"


def _run_checked_local(command: Sequence[str], *, cwd: Path) -> None:
    subprocess.run(list(command), cwd=cwd, check=True, capture_output=True)


def _sync_packaged_rust_artifacts_for_ray_images() -> None:
    subprocess.run(
        [sys.executable, "scripts/sync_packaged_rust_artifacts.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def make_host_project_dir(prefix: str = "project") -> Path:
    HOST_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(HOST_PROJECTS_DIR)))


def init_host_project(
    project_dir: Path,
    *,
    glaas_url: str | None = HOST_GLAAS_URL,
    ignore_tmp_files: bool | None = None,
) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "README.md").write_text("ray host-submit e2e\n", encoding="utf-8")
    (project_dir / ".gitignore").write_text(".roar/\n", encoding="utf-8")
    _run_checked_local(["git", "init", "-q"], cwd=project_dir)
    _run_checked_local(["git", "config", "user.email", "test@test.com"], cwd=project_dir)
    _run_checked_local(["git", "config", "user.name", "test"], cwd=project_dir)
    _run_checked_local(["git", "add", "README.md", ".gitignore"], cwd=project_dir)
    _run_checked_local(["git", "commit", "-q", "-m", "init"], cwd=project_dir)
    _run_checked_local([_roar_bin(), "init", "--path", str(project_dir), "-n"], cwd=project_dir)
    if glaas_url:
        _run_checked_local([_roar_bin(), "config", "set", "glaas.url", glaas_url], cwd=project_dir)
    if ignore_tmp_files is not None:
        _run_checked_local(
            [
                _roar_bin(),
                "config",
                "set",
                "filters.ignore_tmp_files",
                "true" if ignore_tmp_files else "false",
            ],
            cwd=project_dir,
        )


def build_roar_submit_env_from_host(
    ray_cluster: Mapping[str, str],
    *,
    use_fragment_store: bool,
    extra_env: Mapping[str, str] | None = None,
    glaas_url: str = HOST_GLAAS_URL,
    cluster_glaas_url: str = CLUSTER_GLAAS_URL,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ROAR_CLUSTER_PIP_REQ": "skip",
            "AWS_ACCESS_KEY_ID": "minioadmin",
            "AWS_SECRET_ACCESS_KEY": "minioadmin",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": str(ray_cluster["minio_endpoint"]),
            "ROAR_CLUSTER_AWS_ENDPOINT_URL": str(
                ray_cluster.get("cluster_minio_endpoint", "http://minio:9000")
            ),
        }
    )
    if use_fragment_store:
        env["GLAAS_URL"] = glaas_url
        env["ROAR_CLUSTER_GLAAS_URL"] = cluster_glaas_url
    else:
        env["GLAAS_URL"] = ""
        env.pop("ROAR_CLUSTER_GLAAS_URL", None)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return env


def run_roar_ray_job_from_host(
    project_dir: Path,
    ray_cluster: Mapping[str, str],
    script_path: str | Path,
    *,
    use_fragment_store: bool,
    tracer: str | None = "ptrace",
    extra_env: Mapping[str, str] | None = None,
    script_args: Sequence[str] | None = None,
    submit_args: Sequence[str] | None = None,
    working_dir: Path = HOST_JOBS_DIR,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    resolved_script = Path(script_path)
    if resolved_script.is_absolute():
        try:
            script_arg = str(resolved_script.relative_to(working_dir))
        except ValueError:
            script_arg = str(resolved_script)
    else:
        script_arg = str(script_path)

    command = [
        _roar_bin(),
        "run",
    ]
    if tracer:
        command.extend(["--tracer", tracer])
    command.extend(
        [
            "ray",
            "job",
            "submit",
            "--address",
            str(ray_cluster["dashboard_url"]),
            "--working-dir",
            str(working_dir),
        ]
    )
    if submit_args:
        command.extend(submit_args)
    command.extend(["--", "python", script_arg])
    if script_args:
        command.extend(script_args)

    return subprocess.run(
        command,
        cwd=project_dir,
        env=build_roar_submit_env_from_host(
            ray_cluster,
            use_fragment_store=use_fragment_store,
            extra_env=extra_env,
        ),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_roar_cli_from_host(
    project_dir: Path,
    *args: str,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return subprocess.run(
        [_roar_bin(), *args],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def query_roar_db(
    project_dir: Path,
    sql: str,
    params: Sequence[object] = (),
) -> list[dict[str, object]]:
    db_path = project_dir / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def load_fragment_key(project_dir: Path) -> dict[str, str]:
    key_dir = project_dir / ".roar" / "fragment-sessions"
    key_files = sorted(key_dir.glob("*.key"))
    assert key_files, f"Expected a fragment key under {key_dir}"
    payload = json.loads(key_files[-1].read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"Unexpected fragment key payload: {payload!r}"
    return {str(key): str(value) for key, value in payload.items()}


def fetch_fragment_batches(
    session_id: str,
    token: str,
    *,
    glaas_url: str = HOST_GLAAS_URL,
) -> list[dict[str, object]]:
    encoded_token = urllib.parse.quote(token, safe="")
    url = f"{glaas_url.rstrip('/')}/api/v1/fragments/sessions/{session_id}/fragments?token={encoded_token}"
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    fragments = payload.get("fragments")
    if fragments is None and isinstance(payload.get("data"), dict):
        fragments = payload["data"].get("fragments")
    assert isinstance(fragments, list), f"Expected fragment list from {url}, got: {payload!r}"
    return [item for item in fragments if isinstance(item, dict)]


def decrypt_fragment_batches(
    batches: Sequence[dict[str, object]],
    token: str,
) -> list[dict[str, object]]:
    key = bytes.fromhex(token)
    decrypted: list[dict[str, object]] = []
    for batch in batches:
        encrypted_batch = batch.get("encrypted_batch")
        if not isinstance(encrypted_batch, str) or not encrypted_batch:
            continue
        payload = base64.b64decode(encrypted_batch)
        nonce = payload[:12]
        ciphertext = payload[12:]
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        decoded = json.loads(plaintext.decode("utf-8"))
        if isinstance(decoded, list):
            decrypted.extend(item for item in decoded if isinstance(item, dict))
    return decrypted


def exec_shell_on_service(
    service: str,
    cmd: str,
    *,
    compose_file: str | Path = COMPOSE_FILE,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    result = exec_on_service(
        service,
        ["bash", "-lc", cmd],
        compose_file=compose_file,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


def exec_on_head(
    args: Sequence[str],
    *,
    compose_file: str | Path = COMPOSE_FILE,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return exec_on_service(
        "ray-head",
        args,
        compose_file=compose_file,
        env=env,
        timeout=timeout,
    )


def exec_shell_on_head(
    cmd: str,
    *,
    compose_file: str | Path = COMPOSE_FILE,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    return exec_shell_on_service("ray-head", cmd, compose_file=compose_file, env=env)


def reset_roar_project_on_head(
    compose_file: str | Path = COMPOSE_FILE,
    *,
    project_dir: str = HEAD_PROJECT_DIR,
    glaas_url: str | None = FRAGMENT_STORE_URL,
) -> None:
    configure_glaas = ""
    if glaas_url is not None:
        configure_glaas = (
            f" && roar config set glaas.url {shlex.quote(str(glaas_url))}"
            f" && roar config set glaas.web_url {shlex.quote(str(glaas_url))}"
        )
    stdout, stderr, rc = exec_shell_on_head(
        (
            f"cd {shlex.quote(project_dir)}"
            " && git config --global user.email test@test.com"
            " && git config --global user.name test"
            " && git init -q"
            " && git add -A"
            " && git commit -q -m init --allow-empty"
            f" && rm -rf {shlex.quote(project_dir)}/.roar"
            f" && roar init --path {shlex.quote(project_dir)} -n"
            f"{configure_glaas}"
        ),
        compose_file=compose_file,
    )
    if rc != 0:
        raise AssertionError(f"roar init failed on ray-head:\nstdout:\n{stdout}\nstderr:\n{stderr}")


def build_roar_submit_env_on_head(
    *,
    use_fragment_store: bool,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = {
        "AWS_ENDPOINT_URL": "http://minio:9000",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin",
        "ROAR_CLUSTER_PIP_REQ": "skip",
    }
    if use_fragment_store:
        env["GLAAS_URL"] = FRAGMENT_STORE_URL
    else:
        env["GLAAS_URL"] = ""
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return env


def run_roar_ray_job_on_head(
    script_path: str,
    *,
    compose_file: str | Path = COMPOSE_FILE,
    use_fragment_store: bool,
    extra_env: Mapping[str, str] | None = None,
    script_args: Sequence[str] | None = None,
    submit_args: Sequence[str] | None = None,
    working_dir: str = HEAD_PROJECT_DIR,
    timeout: float = 180,
) -> tuple[str, str, int]:
    command = [
        "roar",
        "run",
        "ray",
        "job",
        "submit",
        "--address",
        "http://127.0.0.1:8265",
        "--working-dir",
        working_dir,
    ]
    if submit_args:
        command.extend(submit_args)
    command.extend(["--", "python", script_path])
    if script_args:
        command.extend(script_args)

    result = exec_on_head(
        command,
        compose_file=compose_file,
        env=build_roar_submit_env_on_head(
            use_fragment_store=use_fragment_store,
            extra_env=extra_env,
        ),
        timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


def query_roar_db_on_head(
    sql: str,
    params: Sequence[object] = (),
    *,
    compose_file: str | Path = COMPOSE_FILE,
    db_path: str = f"{HEAD_PROJECT_DIR}/.roar/roar.db",
) -> list[dict[str, object]]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    run_docker(
        [
            "docker",
            "compose",
            "-f",
            str(Path(compose_file)),
            "cp",
            f"ray-head:{db_path}",
            tmp_path,
        ],
        check=True,
        capture_output=True,
    )

    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
        Path(tmp_path).unlink(missing_ok=True)


@pytest.fixture(scope="session")
def ray_cluster() -> dict[str, str]:
    with _exclusive_ray_cluster_lock():
        run_docker(
            _compose_args(COMPOSE_FILE, "down", "-v", "--remove-orphans"),
            check=False,
        )
        _sync_packaged_rust_artifacts_for_ray_images()
        run_docker(
            _compose_args(COMPOSE_FILE, "up", "-d", "--build"),
            check=True,
        )
        try:
            _wait_for_ray_head(COMPOSE_FILE)
            _wait_for_workers(COMPOSE_FILE)
            _ensure_roar_db(COMPOSE_FILE)
            yield {
                "head_address": "ray://localhost:10001",
                "dashboard_url": "http://localhost:8265",
                "minio_endpoint": "http://localhost:9000",
                "cluster_minio_endpoint": "http://minio:9000",
                "compose_file": str(COMPOSE_FILE),
            }
        finally:
            run_docker(
                _compose_args(COMPOSE_FILE, "down", "-v", "--remove-orphans"),
                check=False,
            )


@pytest.fixture(scope="function")
def ray_connection(ray_cluster: dict[str, str]) -> None:
    del ray_cluster
    ray_module = _get_ray()
    ray_module.init(address="ray://localhost:10001", ignore_reinit_error=True)
    try:
        yield
    finally:
        ray_module.shutdown()


def submit_job_on_head(
    compose_file: str | Path,
    script_path: str,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    """Diagnostic helper that bypasses `roar run ray job submit`."""
    compose_path = Path(compose_file)
    merged_env = dict(env or {})

    # When ROAR_WRAP=1, inject PYTHONPATH so sitecustomize.py activates on the
    # driver, and set ROAR_PROJECT_DIR so the collector knows where roar.db lives.
    if merged_env.get("ROAR_WRAP") == "1":
        inject_dir = "/app/roar/services/execution/inject"
        existing_pp = merged_env.get("PYTHONPATH", "")
        merged_env["PYTHONPATH"] = f"{inject_dir}:{existing_pp}" if existing_pp else inject_dir
        merged_env.setdefault("ROAR_PROJECT_DIR", "/app")
        merged_env.setdefault("ROAR_RAY_NODE_AGENTS", "1")

    command = ["docker", "compose", "-f", str(compose_path), "exec", "-T"]
    for key, value in merged_env.items():
        command.extend(["-e", f"{key}={value}"])
    command.extend(["ray-head", "python", script_path])

    result = run_docker(command, capture_output=True, text=True, check=False)
    return result.stdout, result.stderr, result.returncode
