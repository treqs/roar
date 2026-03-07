from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import ray

from roar.ray._agent_names import build_node_agent_name
from roar.services.execution import tracer_backends

_READY_SENTINEL = "ROAR_PROXY_READY"
_DEFAULT_PROXY_START_TIMEOUT_SECONDS = 10.0
__all__ = ["RoarNodeAgent", "build_node_agent_name"]


_ROAR_PROXY_PORT = 19191


def _proxy_port_file_path(job_id: str) -> str:
    """Well-known file path for workers to discover the proxy port without GCS."""
    return f"/tmp/roar-proxy-{job_id}.port"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@ray.remote(num_cpus=0)
class RoarNodeAgent:
    def __init__(self, job_id: str) -> None:
        self._job_id = str(job_id)
        self._proxy_process: subprocess.Popen | None = None
        self._proxy_port: int | None = None
        self._proxy_log_lines: list[str] = []
        self._log_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._node_id = self._runtime_node_id()
        self._start_proxy()

    def _runtime_node_id(self) -> str | None:
        try:
            ctx = ray.get_runtime_context()
            value = ctx.get_node_id()
        except Exception:
            return None

        if value is None:
            return None
        if isinstance(value, bytes):
            return value.hex()

        to_hex = getattr(value, "hex", None)
        if callable(to_hex):
            try:
                return str(to_hex())
            except Exception:
                pass

        return str(value)

    def _start_proxy(self) -> None:
        package_path = Path(__file__).resolve().parents[1]
        proxy_binary = tracer_backends.find_proxy_binary(package_path)
        if not proxy_binary:
            print(f"[roar-agent] roar-proxy binary not found in {package_path}")
            return
        print(f"[roar-agent] found proxy binary: {proxy_binary}")

        port = _find_free_port()
        print(f"[roar-agent] using dynamic port {port} (was hardcoded {_ROAR_PROXY_PORT})")
        cmd = [proxy_binary, "--port", str(port), "--job-id", self._job_id]

        # Only use ROAR_UPSTREAM_S3_ENDPOINT — never fall back to AWS_ENDPOINT_URL.
        # By the time the node agent runs on a worker, AWS_ENDPOINT_URL has been
        # overwritten to http://127.0.0.1:19191 (the proxy itself) by _ray_job_submit.py.
        # Using it as --upstream would make the proxy forward to itself → 502.
        upstream = os.environ.get("ROAR_UPSTREAM_S3_ENDPOINT")
        if upstream:
            cmd.extend(["--upstream", upstream])
            print(f"[roar-agent] upstream: {upstream}")
        else:
            print("[roar-agent] no upstream set, proxy will use default AWS")

        print(f"[roar-agent] starting proxy: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._proxy_process = process

        def _reader() -> None:
            stdout = process.stdout
            if stdout is None:
                return
            for line in stdout:
                with self._log_lock:
                    self._proxy_log_lines.append(line.rstrip("\n"))

        self._reader_thread = threading.Thread(
            target=_reader,
            name="roar-node-agent-proxy-reader",
            daemon=True,
        )
        self._reader_thread.start()

        deadline = time.monotonic() + _DEFAULT_PROXY_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._log_lock:
                ready = any(line.startswith(_READY_SENTINEL) for line in self._proxy_log_lines)
            if ready:
                self._proxy_port = port
                port_path = _proxy_port_file_path(self._job_id)
                try:
                    Path(port_path).write_text(str(port))
                    print(f"[roar-agent] wrote port file {port_path} = {port}")
                except Exception as exc:
                    print(f"[roar-agent] FAILED to write port file {port_path}: {exc}")
                return

            if process.poll() is not None:
                with self._log_lock:
                    output = "\n".join(self._proxy_log_lines[-20:])
                print(f"[roar-agent] proxy process exited early (rc={process.returncode})")
                print(f"[roar-agent] proxy cmd: {' '.join(cmd)}")
                print(f"[roar-agent] proxy output:\n{output}")
                return

            time.sleep(0.05)

        self._terminate_proxy()

    def _cleanup_port_file(self) -> None:
        try:
            Path(_proxy_port_file_path(self._job_id)).unlink(missing_ok=True)
        except Exception:
            pass

    def _terminate_proxy(self) -> None:
        self._cleanup_port_file()
        process = self._proxy_process
        if process is None:
            return

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)

    def get_proxy_port(self) -> int | None:
        return self._proxy_port

    def collect_logs(self) -> dict[str, Any]:
        with self._log_lock:
            log_lines = list(self._proxy_log_lines)

        return {
            "job_id": self._job_id,
            "node_id": self._node_id,
            "proxy_port": self._proxy_port,
            "proxy_log_lines": log_lines,
        }

    def get_log_entries_since(self, since_index: int) -> dict[str, Any]:
        """Return proxy log entries added after since_index."""
        with self._log_lock:
            new_lines = self._proxy_log_lines[since_index:]
            current_index = len(self._proxy_log_lines)
        return {
            "entries": new_lines,
            "current_index": current_index,
            "node_id": self._node_id,
            "proxy_port": self._proxy_port,
        }

    def shutdown(self) -> None:
        self._cleanup_port_file()
        self._terminate_proxy()
