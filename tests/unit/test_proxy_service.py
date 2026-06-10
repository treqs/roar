"""Tests for the ProxyService class."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from roar.execution.cluster.proxy import ProxyHandle, ProxyService


class TestFindProxy:
    def test_finds_release_binary(self, tmp_path):
        proxy_dir = tmp_path / "rust" / "target" / "release"
        proxy_dir.mkdir(parents=True)
        (proxy_dir / "roar-proxy").touch()

        svc = ProxyService(package_path=tmp_path / "roar")
        result = svc.find_proxy()
        assert result is not None
        assert "release" in result

    def test_finds_installed_bin(self, tmp_path):
        bin_dir = tmp_path / "roar" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "roar-proxy").touch()

        svc = ProxyService(package_path=tmp_path / "roar")
        result = svc.find_proxy()
        assert result is not None
        assert "bin" in result

    def test_finds_via_which(self, tmp_path):
        svc = ProxyService(package_path=tmp_path / "roar")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "/usr/local/bin/roar-proxy\n"

        with patch("subprocess.run", return_value=mock_result):
            result = svc.find_proxy()
        assert result == "/usr/local/bin/roar-proxy"

    def test_returns_none_when_not_found(self, tmp_path):
        svc = ProxyService(package_path=tmp_path / "roar")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = svc.find_proxy()
        assert result is None

    def test_prefers_release_over_debug(self, tmp_path):
        for variant in ("release", "debug"):
            proxy_dir = tmp_path / "rust" / "target" / variant
            proxy_dir.mkdir(parents=True)
            (proxy_dir / "roar-proxy").touch()

        svc = ProxyService(package_path=tmp_path / "roar")
        result = svc.find_proxy()
        assert result is not None
        assert "release" in result


class TestStartForRun:
    def test_raises_when_binary_not_found(self, tmp_path):
        svc = ProxyService(package_path=tmp_path / "roar")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(RuntimeError, match="roar-proxy binary not found"),
        ):
            svc.start_for_run()

    def test_passes_upstream_flag(self, tmp_path):
        svc = ProxyService(package_path=tmp_path / "roar")
        svc.find_proxy = MagicMock(return_value="/fake/roar-proxy")

        # Create a mock process that outputs the ready sentinel
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        ready_line = b"ROAR_PROXY_READY port=9999\n"

        # Simulate stdout producing lines
        mock_proc.stdout = iter([ready_line])

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("socket.socket") as mock_socket,
        ):
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock(return_value=False)
            mock_socket.return_value.__enter__.return_value.getsockname.return_value = (
                "127.0.0.1",
                9999,
            )

            _handle = svc.start_for_run(upstream_url="http://localhost:4566")
            cmd = mock_popen.call_args[0][0]
            assert "--upstream" in cmd
            assert "http://localhost:4566" in cmd

    def test_raises_on_timeout(self, tmp_path):
        svc = ProxyService(package_path=tmp_path / "roar")
        svc.find_proxy = MagicMock(return_value="/fake/roar-proxy")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.stdout = iter([])  # No output ever

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("socket.socket") as mock_socket,
            patch("time.monotonic") as mock_time,
        ):
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock(return_value=False)
            mock_socket.return_value.__enter__.return_value.getsockname.return_value = (
                "127.0.0.1",
                9999,
            )
            # Simulate time passing beyond the 5s deadline
            mock_time.side_effect = [0.0, 0.0, 6.0]

            with pytest.raises(RuntimeError, match="did not become ready"):
                svc.start_for_run()


class TestStopForRun:
    def test_parses_log_lines(self):
        proc = MagicMock()
        proc.poll.return_value = 0  # Already exited

        handle = ProxyHandle(
            process=proc,
            port=9090,
            log_lines=[
                "ROAR_PROXY_READY port=9090",
                "[S3:GetObject] s3://bucket/key  etag=abc",
                "[S3:PutObject] s3://bucket/out  (100 bytes)  etag=def",
            ],
        )

        svc = ProxyService()
        entries = svc.stop_for_run(handle)
        assert len(entries) == 2
        assert entries[0].operation == "GetObject"
        assert entries[1].operation == "PutObject"
        assert entries[1].size_bytes == 100

    def test_handles_already_dead_process(self):
        proc = MagicMock()
        proc.poll.return_value = 0

        handle = ProxyHandle(process=proc, port=9090, log_lines=[])
        svc = ProxyService()
        entries = svc.stop_for_run(handle)

        assert entries == []
        proc.terminate.assert_not_called()

    def test_kills_on_timeout(self):
        proc = MagicMock()
        proc.poll.return_value = None  # Still running
        # First wait (with timeout) raises TimeoutExpired, second wait (after kill) succeeds
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 2), None]

        handle = ProxyHandle(process=proc, port=9090, log_lines=[])
        svc = ProxyService()
        svc.stop_for_run(handle)

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


class TestDaemonMethods:
    def test_start_daemon_writes_proxy_json(self, tmp_path):
        svc = ProxyService()
        svc.find_proxy = MagicMock(return_value="/fake/roar-proxy")

        mock_proc = MagicMock()
        mock_proc.pid = 42

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("socket.socket") as mock_socket,
        ):
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock(return_value=False)
            mock_socket.return_value.__enter__.return_value.getsockname.return_value = (
                "127.0.0.1",
                9090,
            )
            info = svc.start_daemon(tmp_path)

        assert info["pid"] == 42
        assert "port" in info
        assert "started_at" in info

    def test_stop_daemon_returns_true_when_running(self, tmp_path):
        import json

        info = {"pid": 99999, "port": 9090, "started_at": 1234567890.0}
        (tmp_path / "proxy.json").write_text(json.dumps(info))

        svc = ProxyService()
        with patch("os.kill"):
            result = svc.stop_daemon(tmp_path)
        assert result is True
        assert not (tmp_path / "proxy.json").exists()

    def test_get_daemon_status_returns_info_when_alive(self, tmp_path):
        import json

        info = {"pid": 99999, "port": 9090, "started_at": 1234567890.0}
        (tmp_path / "proxy.json").write_text(json.dumps(info))

        svc = ProxyService()
        with patch("os.kill"):  # os.kill(pid, 0) succeeds
            result = svc.get_daemon_status(tmp_path)
        assert result is not None
        assert result["pid"] == 99999

    def test_get_daemon_status_returns_none_and_cleans_up_when_dead(self, tmp_path):
        import json

        info = {"pid": 99999, "port": 9090, "started_at": 1234567890.0}
        (tmp_path / "proxy.json").write_text(json.dumps(info))

        svc = ProxyService()
        with patch("os.kill", side_effect=OSError("No such process")):
            result = svc.get_daemon_status(tmp_path)
        assert result is None
        assert not (tmp_path / "proxy.json").exists()
