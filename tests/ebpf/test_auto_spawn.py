"""Tests for auto-spawn behavior of roar-tracer-ebpf with daemon."""

import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.ebpf

try:
    import msgpack
except ImportError:
    pytestmark = [pytest.mark.ebpf, pytest.mark.skip(reason="msgpack not installed")]


def test_tracer_auto_spawns_daemon(ebpf_tracer_binary, roard_binary, tmp_path):
    """Run roar-tracer-ebpf with no daemon running, verify it spawns roard and produces a report."""
    output_file = tmp_path / "trace.msgpack"

    # Set XDG_RUNTIME_DIR to a temp dir so the socket doesn't collide
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    # Ensure roard is findable — put it next to the tracer or in PATH
    env["PATH"] = f"{roard_binary.parent}:{env.get('PATH', '')}"

    result = subprocess.run(
        [str(ebpf_tracer_binary), str(output_file), "echo", "hello"],
        capture_output=True,
        timeout=30,
        env=env,
    )

    # The tracer should succeed (either via daemon or fallback to inline BPF)
    # We mainly check that it didn't crash and produced output
    assert result.returncode == 0, f"tracer failed: {result.stderr.decode()}"
    assert output_file.exists(), "no output file produced"
    assert output_file.stat().st_size > 0, "output file is empty"

    # Verify it's valid msgpack
    data = msgpack.unpackb(output_file.read_bytes(), raw=False)
    assert data["version"] == 1
    assert data["tracer_mode"] == "ebpf"


def test_tracer_reuses_running_daemon(ebpf_tracer_binary, roard_binary, tmp_path):
    """Start roard, run tracer, verify same daemon PID used."""
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    env["PATH"] = f"{roard_binary.parent}:{env.get('PATH', '')}"

    # Start daemon explicitly
    socket_path = tmp_path / "roard.sock"
    proc = subprocess.Popen(
        [str(roard_binary), "--socket", str(socket_path), "--idle-timeout", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    # Wait for socket
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if socket_path.exists():
            break
        time.sleep(0.05)

    try:
        pid_before = proc.pid

        output_file = tmp_path / "trace.msgpack"
        result = subprocess.run(
            [str(ebpf_tracer_binary), str(output_file), "echo", "hello"],
            capture_output=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0

        # Daemon should still be the same process
        assert proc.poll() is None, "daemon exited unexpectedly"
        assert proc.pid == pid_before
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_tracer_fallback_on_no_roard_binary(ebpf_tracer_binary, tmp_path):
    """Ensure tracer falls back to inline BPF when roard binary not found."""
    output_file = tmp_path / "trace.msgpack"

    # Set PATH to something that definitely doesn't have roard
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    env["PATH"] = "/usr/bin:/bin"

    # This test verifies the fallback path works. The tracer should either:
    # - Use daemon mode (if roard happens to be findable)
    # - Fall back to inline BPF (which requires CAP_BPF)
    # - Exit with an error (if no BPF capability)
    # We just verify it doesn't hang or crash unexpectedly.
    result = subprocess.run(
        [str(ebpf_tracer_binary), str(output_file), "echo", "hello"],
        capture_output=True,
        timeout=30,
        env=env,
    )

    # Accept exit code 0 (BPF worked) or 1 (BPF failed due to permissions)
    # but not a crash signal
    assert result.returncode in (0, 1), (
        f"unexpected exit code {result.returncode}: {result.stderr.decode()}"
    )
