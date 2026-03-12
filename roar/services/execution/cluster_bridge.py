from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roar.services.execution import tracer_backends

READY_SENTINEL = "ROAR_PROXY_READY"
DEFAULT_PROXY_START_TIMEOUT_SECONDS = 10.0


@dataclass
class SidecarHandle:
    port: int
    process: subprocess.Popen | None = None
    log_lines: list[str] = field(default_factory=list)
    reader_thread: threading.Thread | None = None


def can_connect_to_local_proxy(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.25):
        return True
    return False


def proxy_claim_root(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    configured = str(resolved_env.get("ROAR_PROXY_CLAIM_DIR", "")).strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "roar-ray-proxy-claims"


def proxy_claim_path(port: int, env: Mapping[str, str] | None = None) -> Path:
    return proxy_claim_root(env) / f"{int(port)}.json"


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    with contextlib.suppress(OSError):
        os.kill(pid, 0)
        return True
    return False


def load_proxy_claim(port: int, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    path = proxy_claim_path(port, env)
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_proxy_claim(
    port: int,
    *,
    pid: int | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    path = proxy_claim_path(port, env)
    if pid is not None:
        claim = load_proxy_claim(port, env)
        if not isinstance(claim, dict) or int(claim.get("pid") or 0) != pid:
            return
    with contextlib.suppress(OSError):
        path.unlink()


def write_proxy_claim(
    port: int,
    *,
    job_id: str,
    upstream: str | None,
    pid: int,
    env: Mapping[str, str] | None = None,
) -> None:
    path = proxy_claim_path(port, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": str(job_id),
        "upstream": str(upstream or ""),
        "pid": int(pid),
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def proxy_claim_matches(
    port: int,
    *,
    job_id: str,
    upstream: str | None,
    env: Mapping[str, str] | None = None,
) -> bool:
    claim = load_proxy_claim(port, env)
    if claim is None:
        return False

    pid = int(claim.get("pid") or 0)
    if not process_is_alive(pid):
        clear_proxy_claim(port, env=env)
        return False

    return str(claim.get("job_id") or "") == str(job_id) and str(
        claim.get("upstream") or ""
    ) == str(upstream or "")


class LocalProxyClusterBridge:
    def __init__(
        self,
        package_path: Path,
        *,
        message_sink: Callable[[str], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._package_path = package_path
        self._message_sink = message_sink or (lambda message: None)
        self._env = os.environ if env is None else env

    def start(self, *, job_id: str, port: int, upstream_url: str | None) -> SidecarHandle | None:
        proxy_binary = tracer_backends.find_proxy_binary(self._package_path)
        if not proxy_binary:
            self._message_sink(f"[roar-bridge] roar-proxy binary not found in {self._package_path}")
            return None

        if can_connect_to_local_proxy(port):
            if proxy_claim_matches(
                port,
                job_id=job_id,
                upstream=upstream_url,
                env=self._env,
            ):
                self._message_sink(f"[roar-bridge] reusing owned local proxy on port {port}")
                return SidecarHandle(port=port)

            self._message_sink(
                f"[roar-bridge] refusing to reuse existing listener on port {port}: "
                "proxy ownership could not be verified"
            )
            return None

        cmd = [proxy_binary, "--port", str(port), "--job-id", str(job_id)]
        if upstream_url:
            cmd.extend(["--upstream", upstream_url])
            self._message_sink(f"[roar-bridge] upstream: {upstream_url}")
        else:
            self._message_sink("[roar-bridge] no upstream set, proxy will use default AWS")

        self._message_sink(f"[roar-bridge] starting proxy: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        handle = SidecarHandle(port=port, process=process)

        def _reader() -> None:
            stdout = process.stdout
            if stdout is None:
                return
            for line in stdout:
                handle.log_lines.append(line.rstrip("\n"))

        handle.reader_thread = threading.Thread(
            target=_reader,
            name="roar-proxy-sidecar-reader",
            daemon=True,
        )
        handle.reader_thread.start()

        deadline = time.monotonic() + DEFAULT_PROXY_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if any(line.startswith(READY_SENTINEL) for line in handle.log_lines):
                write_proxy_claim(
                    port,
                    job_id=job_id,
                    upstream=upstream_url,
                    pid=process.pid,
                    env=self._env,
                )
                return handle

            if process.poll() is not None:
                output = "\n".join(handle.log_lines[-20:])
                self._message_sink(
                    f"[roar-bridge] proxy process exited early (rc={process.returncode})"
                )
                self._message_sink(f"[roar-bridge] proxy cmd: {' '.join(cmd)}")
                self._message_sink(f"[roar-bridge] proxy output:\n{output}")
                return None

            time.sleep(0.05)

        self.stop(handle)
        return None

    def stop(self, handle: SidecarHandle | None) -> None:
        if handle is None or handle.process is None:
            return

        process = handle.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        if handle.reader_thread is not None and handle.reader_thread.is_alive():
            handle.reader_thread.join(timeout=2)

        clear_proxy_claim(handle.port, pid=process.pid, env=self._env)

    def log_lines(self, handle: SidecarHandle | None) -> list[str]:
        if handle is None:
            return []
        return list(handle.log_lines)
