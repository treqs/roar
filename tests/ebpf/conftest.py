"""Fixtures for eBPF daemon integration tests."""

import os
import socket
import struct
import subprocess
import time
from pathlib import Path

import pytest

# Possible locations for the built binaries
_SEARCH_DIRS = [
    Path(__file__).resolve().parents[2] / "tracer-ebpf" / "target" / "release",
    Path(__file__).resolve().parents[2] / "tracer-ebpf" / "target" / "debug",
]


def _find_binary(name: str) -> Path | None:
    for d in _SEARCH_DIRS:
        p = d / name
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


@pytest.fixture
def roard_binary():
    """Path to the roard binary, or skip if not built."""
    p = _find_binary("roard")
    if p is None:
        pytest.skip("roard binary not built")
    return p


@pytest.fixture
def ebpf_tracer_binary():
    """Path to roar-tracer-ebpf binary, or skip if not built."""
    p = _find_binary("roar-tracer-ebpf")
    if p is None:
        pytest.skip("roar-tracer-ebpf binary not built")
    return p


@pytest.fixture
def daemon_socket(tmp_path):
    """Unique temp socket path per test (avoids collisions)."""
    return tmp_path / "roard-test.sock"


@pytest.fixture
def roard_process(roard_binary, daemon_socket):
    """Start roard with a short idle timeout, yield, and clean up."""
    proc = subprocess.Popen(
        [
            str(roard_binary),
            "--socket",
            str(daemon_socket),
            "--idle-timeout",
            "30",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the socket to appear (up to 2s)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if daemon_socket.exists():
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("roard did not create socket within 2s")

    yield proc

    # Teardown: kill the daemon
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ── Helpers for IPC from Python ──────────────────────────────────────────────

try:
    import msgpack

    HAS_MSGPACK = True
except ImportError:
    HAS_MSGPACK = False


def send_ipc_message(sock: socket.socket, msg: dict) -> None:
    """Send a length-prefixed MessagePack message over a Unix socket."""
    payload = msgpack.packb(msg, use_bin_type=True)
    sock.sendall(struct.pack(">I", len(payload)))
    sock.sendall(payload)


def recv_ipc_message(sock: socket.socket) -> dict:
    """Receive a length-prefixed MessagePack message from a Unix socket."""
    raw_len = _recv_exact(sock, 4)
    length = struct.unpack(">I", raw_len)[0]
    payload = _recv_exact(sock, length)
    return msgpack.unpackb(payload, raw=False)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("unexpected EOF on IPC socket")
        buf += chunk
    return buf


def connect_ipc(socket_path: Path) -> socket.socket:
    """Connect to a roard Unix socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(socket_path))
    return s
