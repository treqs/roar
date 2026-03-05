"""Pytest fixtures for the Docker-based Ray test harness."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import http.server
import importlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
HEAD_TIMEOUT_SECONDS = 120
WORKERS_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 3
FRAGMENT_GLAAS_URL_ENV = "ROAR_E2E_FRAGMENT_GLAAS_URL"

os.environ.setdefault(FRAGMENT_GLAAS_URL_ENV, "http://localhost:3301")


class _FragmentStoreHandler(http.server.BaseHTTPRequestHandler):
    sessions: dict[str, dict[str, object]] = {}
    lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _route_parts(self) -> tuple[list[str], urllib.parse.ParseResult]:
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        return parts, parsed

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def do_GET(self) -> None:  # noqa: N802
        parts, parsed = self._route_parts()
        if parts == ["api", "v1", "health"]:
            self._write_json(200, {"success": True, "status": "healthy"})
            return

        if len(parts) == 6 and parts[:4] == ["api", "v1", "fragments", "sessions"] and parts[5] == "fragments":
            session_id = parts[4]
            token = urllib.parse.parse_qs(parsed.query).get("token", [None])[0]
            if not token:
                token = self.headers.get("x-roar-fragment-token")
            if not token:
                self._write_json(401, {"success": False, "error": "missing token"})
                return

            with self.lock:
                session = self.sessions.get(session_id)
                if not isinstance(session, dict):
                    self._write_json(404, {"success": False, "error": "unknown session"})
                    return
                expected_hash = str(session.get("token_hash") or "")
                if expected_hash and self._token_hash(str(token)) != expected_hash:
                    self._write_json(403, {"success": False, "error": "invalid token"})
                    return
                fragments = list(session.get("fragments", []))

            self._write_json(
                200,
                {
                    "success": True,
                    "data": {"fragments": fragments},
                    "fragments": fragments,
                },
            )
            return

        self._write_json(404, {"success": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parts, _parsed = self._route_parts()
        body = self._read_json_body()

        if parts == ["api", "v1", "fragments", "sessions"]:
            session_id = str(body.get("session_id") or "")
            token_hash = str(body.get("token_hash") or "")
            if not session_id or not token_hash:
                self._write_json(400, {"success": False, "error": "missing fields"})
                return

            with self.lock:
                self.sessions.setdefault(session_id, {"token_hash": token_hash, "fragments": []})
            self._write_json(200, {"success": True, "data": {"session_id": session_id}})
            return

        if len(parts) == 6 and parts[:4] == ["api", "v1", "fragments", "sessions"] and parts[5] == "fragments":
            session_id = parts[4]
            token = self.headers.get("x-roar-fragment-token", "")
            encrypted_batch = body.get("encrypted_batch")
            sequence = body.get("sequence")
            if not isinstance(encrypted_batch, str) or not encrypted_batch:
                self._write_json(400, {"success": False, "error": "missing encrypted_batch"})
                return

            with self.lock:
                session = self.sessions.get(session_id)
                if not isinstance(session, dict):
                    self._write_json(404, {"success": False, "error": "unknown session"})
                    return
                expected_hash = str(session.get("token_hash") or "")
                if expected_hash and self._token_hash(token) != expected_hash:
                    self._write_json(403, {"success": False, "error": "invalid token"})
                    return
                fragments = session.setdefault("fragments", [])
                if not isinstance(fragments, list):
                    fragments = []
                    session["fragments"] = fragments
                fragments.append({"encrypted_batch": encrypted_batch, "sequence": sequence})

            self._write_json(200, {"success": True})
            return

        self._write_json(404, {"success": False, "error": "not found"})


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
    repo_root = COMPOSE_FILE.parent.parent.parent.parent.resolve()
    venv_bin = repo_root / ".venv" / "bin"
    if venv_bin.exists():
        current_path = os.environ.get("PATH", "")
        venv_bin_text = str(venv_bin)
        if venv_bin_text not in current_path.split(":"):
            os.environ["PATH"] = f"{venv_bin_text}:{current_path}" if current_path else venv_bin_text

    config.option.importmode = "importlib"
    with contextlib.suppress(
        ModuleNotFoundError
    ):  # Ray not installed; e2e tests require a live Docker cluster
        _get_ray()
    config.addinivalue_line("markers", "ray_e2e: Ray end-to-end tests requiring Docker")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.ray_e2e
    for item in items:
        item.add_marker(marker)


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


@pytest.fixture(scope="session")
def ray_cluster() -> dict[str, str]:
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
            "compose_file": str(COMPOSE_FILE),
        }
    finally:
        run_docker(
            _compose_args(COMPOSE_FILE, "down", "-v"),
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
    compose_path = Path(compose_file)
    merged_env = dict(env or {})

    # When ROAR_WRAP=1, inject PYTHONPATH so sitecustomize.py activates on the
    # driver, and set ROAR_PROJECT_DIR so the collector knows where roar.db lives.
    if merged_env.get("ROAR_WRAP") == "1":
        inject_dir = "/app/roar/services/execution/inject"
        existing_pp = merged_env.get("PYTHONPATH", "")
        merged_env["PYTHONPATH"] = f"{inject_dir}:{existing_pp}" if existing_pp else inject_dir
        merged_env.setdefault("ROAR_PROJECT_DIR", "/app")
        merged_env.setdefault("ROAR_LOG_DIR", "/shared/.roar-logs")
        merged_env.setdefault("ROAR_RAY_NODE_AGENTS", "1")

    command = ["docker", "compose", "-f", str(compose_path), "exec", "-T"]
    for key, value in merged_env.items():
        command.extend(["-e", f"{key}={value}"])
    if merged_env.get("ROAR_WRAP") == "1":
        log_dir = merged_env.get("ROAR_LOG_DIR", "/shared/.roar-logs")
        # sitecustomize currently checks for a *.json sentinel before collecting.
        # Create one so actor-backed fragments are always flushed into roar.db.
        trigger_path = f"{str(log_dir).rstrip('/')}/collector-trigger.json"
        shell_command = (
            f"mkdir -p {shlex.quote(str(log_dir))} "
            f"&& : > {shlex.quote(trigger_path)} "
            f"&& python {shlex.quote(str(script_path))}"
        )
        command.extend(["ray-head", "bash", "-lc", shell_command])
    else:
        command.extend(["ray-head", "python", script_path])

    result = run_docker(command, capture_output=True, text=True, check=False)
    return result.stdout, result.stderr, result.returncode
