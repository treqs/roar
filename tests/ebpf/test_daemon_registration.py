"""Tests for daemon registration and report retrieval."""

import os

import pytest

pytestmark = pytest.mark.ebpf

try:
    import msgpack  # noqa: F401
except ImportError:
    pytestmark = [pytest.mark.ebpf, pytest.mark.skip(reason="msgpack not installed")]


def test_register_and_get_stub_report(roard_process, daemon_socket):
    """Register → Ack → Deregister → GetReport → verify stub Report."""
    from tests.ebpf.conftest import connect_ipc, recv_ipc_message, send_ipc_message

    sock = connect_ipc(daemon_socket)
    try:
        run_id = 12345

        # Register
        send_ipc_message(sock, {"type": "Register", "run_id": run_id, "root_pid": os.getpid()})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Ack"
        assert resp["run_id"] == run_id

        # Deregister
        send_ipc_message(sock, {"type": "Deregister", "run_id": run_id})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Ack"

        # GetReport
        send_ipc_message(sock, {"type": "GetReport", "run_id": run_id})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Report"
        assert resp["run_id"] == run_id

        data = resp["data"]
        assert data["version"] == 1
        assert data["tracer_mode"] == "ebpf"
        assert data["start_time"] > 0
        assert data["end_time"] >= data["start_time"]
    finally:
        sock.close()


def test_concurrent_registrations(roard_process, daemon_socket):
    """Two clients register with different run_ids, each gets independent report."""
    from tests.ebpf.conftest import connect_ipc, recv_ipc_message, send_ipc_message

    sock1 = connect_ipc(daemon_socket)
    sock2 = connect_ipc(daemon_socket)
    try:
        # Register two runs
        send_ipc_message(sock1, {"type": "Register", "run_id": 100, "root_pid": 9001})
        resp1 = recv_ipc_message(sock1)
        assert resp1["type"] == "Ack"

        send_ipc_message(sock2, {"type": "Register", "run_id": 200, "root_pid": 9002})
        resp2 = recv_ipc_message(sock2)
        assert resp2["type"] == "Ack"

        # Deregister both
        send_ipc_message(sock1, {"type": "Deregister", "run_id": 100})
        recv_ipc_message(sock1)
        send_ipc_message(sock2, {"type": "Deregister", "run_id": 200})
        recv_ipc_message(sock2)

        # Get reports — each should succeed independently
        send_ipc_message(sock1, {"type": "GetReport", "run_id": 100})
        report1 = recv_ipc_message(sock1)
        assert report1["type"] == "Report"
        assert report1["run_id"] == 100

        send_ipc_message(sock2, {"type": "GetReport", "run_id": 200})
        report2 = recv_ipc_message(sock2)
        assert report2["type"] == "Report"
        assert report2["run_id"] == 200
    finally:
        sock1.close()
        sock2.close()


def test_deregister_one_keeps_others(roard_process, daemon_socket):
    """Register 3 runs, deregister middle one, verify others still exist."""
    from tests.ebpf.conftest import connect_ipc, recv_ipc_message, send_ipc_message

    sock = connect_ipc(daemon_socket)
    try:
        # Register 3 runs
        for rid in [10, 20, 30]:
            send_ipc_message(sock, {"type": "Register", "run_id": rid, "root_pid": 8000 + rid})
            resp = recv_ipc_message(sock)
            assert resp["type"] == "Ack"

        # Deregister run 20
        send_ipc_message(sock, {"type": "Deregister", "run_id": 20})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Ack"

        # GetReport for run 20 should succeed (completed → report available)
        send_ipc_message(sock, {"type": "GetReport", "run_id": 20})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Report"

        # Deregister and get report for run 10 — should still work
        send_ipc_message(sock, {"type": "Deregister", "run_id": 10})
        recv_ipc_message(sock)
        send_ipc_message(sock, {"type": "GetReport", "run_id": 10})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Report"
        assert resp["run_id"] == 10

        # Run 30 should still be available too
        send_ipc_message(sock, {"type": "Deregister", "run_id": 30})
        recv_ipc_message(sock)
        send_ipc_message(sock, {"type": "GetReport", "run_id": 30})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Report"
        assert resp["run_id"] == 30
    finally:
        sock.close()


def test_get_report_returns_metadata(roard_process, daemon_socket):
    """Verify stub report has correct run_id, start_time, tracer_mode."""
    from tests.ebpf.conftest import connect_ipc, recv_ipc_message, send_ipc_message

    sock = connect_ipc(daemon_socket)
    try:
        run_id = 99999
        send_ipc_message(sock, {"type": "Register", "run_id": run_id, "root_pid": 7777})
        recv_ipc_message(sock)

        send_ipc_message(sock, {"type": "Deregister", "run_id": run_id})
        recv_ipc_message(sock)

        send_ipc_message(sock, {"type": "GetReport", "run_id": run_id})
        resp = recv_ipc_message(sock)

        assert resp["type"] == "Report"
        assert resp["run_id"] == run_id

        data = resp["data"]
        assert data["tracer_mode"] == "ebpf"
        assert data["version"] == 1
        assert isinstance(data["start_time"], float)
        assert isinstance(data["end_time"], float)
        assert data["events_dropped"] == 0
        assert isinstance(data["files"], list)
        assert isinstance(data["processes"], list)
    finally:
        sock.close()


def test_get_report_unknown_run_id(roard_process, daemon_socket):
    """GetReport for a non-existent run_id should return an Error."""
    from tests.ebpf.conftest import connect_ipc, recv_ipc_message, send_ipc_message

    sock = connect_ipc(daemon_socket)
    try:
        send_ipc_message(sock, {"type": "GetReport", "run_id": 777777})
        resp = recv_ipc_message(sock)
        assert resp["type"] == "Error"
        assert resp["run_id"] == 777777
        assert "unknown" in resp["message"].lower()
    finally:
        sock.close()
