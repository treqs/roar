"""Tests for daemon lifecycle: start, ping, idle timeout, stale socket."""

import socket
import subprocess
import time

import pytest

pytestmark = pytest.mark.ebpf

try:
    import msgpack  # noqa: F401
except ImportError:
    pytestmark = [pytest.mark.ebpf, pytest.mark.skip(reason="msgpack not installed")]


def test_daemon_starts_and_accepts_ping(roard_process, daemon_socket):
    """Spawn roard, connect, send Ping, receive Pong."""
    from tests.ebpf.conftest import connect_ipc, recv_ipc_message, send_ipc_message

    sock = connect_ipc(daemon_socket)
    try:
        send_ipc_message(sock, {"type": "Ping"})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Pong"
    finally:
        sock.close()


def test_daemon_idle_timeout(roard_binary, daemon_socket):
    """Spawn roard with a 2s timeout, verify it exits after ~2s."""
    proc = subprocess.Popen(
        [
            str(roard_binary),
            "--socket",
            str(daemon_socket),
            "--idle-timeout",
            "2",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for socket to appear
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if daemon_socket.exists():
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("roard did not create socket")

    # Daemon should exit within ~3s (2s timeout + margin)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("roard did not exit after idle timeout")

    assert proc.returncode == 0


def test_stale_socket_cleanup(roard_binary, daemon_socket):
    """Create a stale socket file, verify roard starts despite it."""
    # Create a fake socket file (nobody listening)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(daemon_socket))
    stale.close()

    assert daemon_socket.exists()

    # roard should detect the stale socket, clean it up, and start
    proc = subprocess.Popen(
        [
            str(roard_binary),
            "--socket",
            str(daemon_socket),
            "--idle-timeout",
            "2",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the daemon to start (it should replace the stale socket)
    deadline = time.monotonic() + 3.0
    started = False
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(daemon_socket))
            s.close()
            started = True
            break
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            time.sleep(0.05)

    if not started:
        proc.kill()
        pytest.fail("roard did not start after stale socket cleanup")

    proc.terminate()
    proc.wait(timeout=5)


def test_daemon_rejects_duplicate(roard_process, roard_binary, daemon_socket):
    """Spawn roard, attempt to spawn another on same socket, verify error."""
    proc2 = subprocess.Popen(
        [
            str(roard_binary),
            "--socket",
            str(daemon_socket),
            "--idle-timeout",
            "10",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Second instance should exit with error
    try:
        proc2.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc2.kill()
        pytest.fail("second roard instance did not exit")

    assert proc2.returncode != 0
    stderr = proc2.stderr.read().decode()
    assert "another instance" in stderr or "Address already in use" in stderr
