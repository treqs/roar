"""
S3 proxy service for lineage tracking.

Manages the roar-proxy binary lifecycle for intercepting S3 operations
and recording them as lineage (inputs/outputs) during `roar run`.
"""

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...core.interfaces.logger import ILogger


@dataclass
class ProxyHandle:
    """Handle to a running per-run proxy process."""

    process: subprocess.Popen
    port: int
    log_lines: list[str] = field(default_factory=list)
    reader_thread: threading.Thread | None = None


@dataclass
class S3LogEntry:
    """Parsed S3 operation from proxy log output."""

    operation: str  # "GetObject", "PutObject", etc.
    bucket: str
    key: str
    etag: str | None = None
    byte_ranges: list[list[int]] | None = None
    size_bytes: int | None = None


# Pattern for log lines: [S3:OpType] s3://bucket/key  optional-metadata
_LOG_RE = re.compile(r"^\[S3:(?P<op>\w+)\]\s+s3://(?P<bucket>[^/]+)/(?P<key>\S+)")


def parse_log_line(line: str) -> S3LogEntry | None:
    """Parse a proxy stdout log line into an S3LogEntry.

    Log format (fields separated by double-space):
      [S3:GetObject] s3://bucket/key  etag=abc123  session=s1  job=j1
      [S3:PutObject] s3://bucket/key  (1234 bytes)  etag=abc123
    """
    m = _LOG_RE.match(line)
    if not m:
        return None

    op = m.group("op")
    bucket = m.group("bucket")
    key = m.group("key")

    etag = None
    byte_ranges = None
    size_bytes = None

    # Parse key=value pairs from the rest of the line
    rest = line[m.end() :]
    for segment in rest.split("  "):
        segment = segment.strip()
        if not segment:
            continue
        if segment.startswith("etag="):
            etag = segment[5:]
        elif segment.startswith("byte_ranges="):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                byte_ranges = json.loads(segment[len("byte_ranges=") :])
        elif segment.startswith("(") and segment.endswith(" bytes)"):
            with contextlib.suppress(ValueError):
                size_bytes = int(segment[1:].split()[0])

    return S3LogEntry(
        operation=op,
        bucket=bucket,
        key=key,
        etag=etag,
        byte_ranges=byte_ranges,
        size_bytes=size_bytes,
    )


class ProxyService:
    """
    Manages roar-proxy binary discovery and lifecycle.

    Follows SRP: only handles proxy process management.
    """

    def __init__(self, package_path: Path | None = None, logger: ILogger | None = None) -> None:
        # Go up 3 levels: execution -> services -> roar
        self._package_path = package_path or Path(__file__).parent.parent.parent
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        if self._logger is None:
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def find_proxy(self) -> str | None:
        """Find the roar-proxy binary.

        Search order:
          1. ../rust/target/release/roar-proxy  (dev build)
          2. ../rust/target/debug/roar-proxy    (dev debug build)
          3. ./bin/roar-proxy                      (installed)
          4. PATH via `which`
        """
        candidates = [
            self._package_path.parent / "rust" / "target" / "release" / "roar-proxy",
            self._package_path.parent / "rust" / "target" / "debug" / "roar-proxy",
            self._package_path / "bin" / "roar-proxy",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        result = subprocess.run(["which", "roar-proxy"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def start_for_run(
        self,
        session_id: str | None = None,
        job_id: str | None = None,
        upstream_url: str | None = None,
        port: int | None = None,
    ) -> ProxyHandle:
        """Start a proxy for a single `roar run`.

        Finds a free port, starts the proxy subprocess, waits for the
        ROAR_PROXY_READY sentinel, and returns a ProxyHandle.

        Raises:
            RuntimeError: If the proxy binary is not found or fails to start.
        """
        proxy_path = self.find_proxy()
        if not proxy_path:
            raise RuntimeError(
                "roar-proxy binary not found. Build it with:\n"
                "  cargo build --release --manifest-path rust/Cargo.toml -p roar-proxy"
            )

        if port is None:
            # Find a free port
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]

        cmd = [proxy_path, "--port", str(port)]
        if session_id:
            cmd.extend(["--session-id", session_id])
        if job_id:
            cmd.extend(["--job-id", job_id])
        if upstream_url:
            cmd.extend(["--upstream", upstream_url])

        self.logger.debug("Starting proxy: %s", cmd)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        handle = ProxyHandle(process=proc, port=port)

        # Spawn a reader thread that accumulates stdout lines
        def _reader():
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                handle.log_lines.append(line)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        handle.reader_thread = reader

        # Wait for the ROAR_PROXY_READY sentinel
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            for line in handle.log_lines:
                if line.startswith("ROAR_PROXY_READY"):
                    self.logger.debug("Proxy ready on port %d", port)
                    return handle
            # Check if process died
            if proc.poll() is not None:
                stderr_out = (
                    proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                )
                raise RuntimeError(
                    f"roar-proxy exited with code {proc.returncode} before becoming ready.\n"
                    f"stderr: {stderr_out}"
                )
            time.sleep(0.05)

        # Timeout — kill and raise
        proc.kill()
        proc.wait()
        raise RuntimeError("roar-proxy did not become ready within 5 seconds")

    def stop_for_run(self, handle: ProxyHandle) -> list[S3LogEntry]:
        """Stop a per-run proxy and return parsed S3 log entries.

        Sends SIGTERM, waits up to 2s, then SIGKILL if needed.
        Joins the reader thread and parses accumulated log lines.
        """
        proc = handle.process
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        if handle.reader_thread and handle.reader_thread.is_alive():
            handle.reader_thread.join(timeout=2)

        entries = []
        for line in handle.log_lines:
            entry = parse_log_line(line)
            if entry:
                entries.append(entry)

        return entries

    def start_daemon(self, roar_dir: Path) -> dict:
        """Start a standalone proxy daemon.

        Writes PID/port info to .roar/proxy.json.
        Stdout/stderr go to .roar/proxy.log.

        Returns:
            Dict with 'pid', 'port', 'started_at' keys.
        """
        proxy_path = self.find_proxy()
        if not proxy_path:
            raise RuntimeError(
                "roar-proxy binary not found. Build it with:\n"
                "  cargo build --release --manifest-path rust/Cargo.toml -p roar-proxy"
            )

        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        log_path = roar_dir / "proxy.log"
        log_file = open(log_path, "w")  # noqa: SIM115

        proc = subprocess.Popen(
            [proxy_path, "--port", str(port)],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

        info = {
            "pid": proc.pid,
            "port": port,
            "started_at": time.time(),
        }
        (roar_dir / "proxy.json").write_text(json.dumps(info))

        return info

    def stop_daemon(self, roar_dir: Path) -> bool:
        """Stop a running proxy daemon.

        Returns True if a daemon was stopped, False if none was running.
        """
        info_path = roar_dir / "proxy.json"
        if not info_path.exists():
            return False

        try:
            info = json.loads(info_path.read_text())
            pid = info["pid"]
            os.kill(pid, signal.SIGTERM)
            # Brief wait for clean shutdown
            for _ in range(20):
                try:
                    os.kill(pid, 0)  # Check if still alive
                    time.sleep(0.1)
                except OSError:
                    break
        except (OSError, KeyError, json.JSONDecodeError):
            pass

        # Clean up state files
        info_path.unlink(missing_ok=True)
        return True

    def get_daemon_status(self, roar_dir: Path) -> dict | None:
        """Check if a proxy daemon is alive.

        Returns:
            Dict with daemon info if running, None otherwise.
        """
        info_path = roar_dir / "proxy.json"
        if not info_path.exists():
            return None

        try:
            info = json.loads(info_path.read_text())
            pid = info["pid"]
            os.kill(pid, 0)  # Check if process exists
            return info
        except (OSError, KeyError, json.JSONDecodeError):
            # Process dead or file corrupt — clean up
            info_path.unlink(missing_ok=True)
            return None
