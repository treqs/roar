"""Tests for proxy CLI branches that are not worth hitting via subprocess."""

import importlib
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# Import the proxy module to patch against
proxy_cli_module = importlib.import_module("roar.cli.commands.proxy")


class TestProxyCli:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_proxy_enable_surfaces_config_errors(self, runner):
        with patch.object(proxy_cli_module, "config_set", side_effect=ValueError("bad config")):
            result = runner.invoke(proxy_cli_module.proxy, ["enable"])

        assert result.exit_code != 0
        assert "bad config" in result.output

    def test_proxy_disable_surfaces_config_errors(self, runner):
        with patch.object(proxy_cli_module, "config_set", side_effect=ValueError("bad config")):
            result = runner.invoke(proxy_cli_module.proxy, ["disable"])

        assert result.exit_code != 0
        assert "bad config" in result.output

    def test_proxy_status_handles_daemon_lookup_errors(self, runner):
        mock_svc = MagicMock()
        mock_svc.find_proxy.return_value = None
        mock_svc.get_daemon_status.side_effect = RuntimeError("boom")

        with (
            patch.object(proxy_cli_module, "config_get", return_value=False),
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy)

        assert result.exit_code == 0
        assert "Proxy enabled: False" in result.output
        assert "Binary: not found" in result.output
        assert "Daemon: not running" in result.output

    def test_proxy_start_when_already_running(self, runner):
        mock_svc = MagicMock()
        mock_svc.get_daemon_status.return_value = {"pid": 123, "port": 9090}

        with (
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy, ["start"])
            assert result.exit_code == 0
            assert "already running" in result.output.lower()
            mock_svc.start_daemon.assert_not_called()

    def test_proxy_stop_when_not_running(self, runner):
        mock_svc = MagicMock()
        mock_svc.stop_daemon.return_value = False

        with (
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy, ["stop"])
            assert result.exit_code == 0
            assert "no proxy daemon" in result.output.lower()
