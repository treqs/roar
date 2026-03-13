"""Tests for the proxy CLI commands."""

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

    def test_proxy_no_subcommand_shows_status(self, runner):
        mock_svc = MagicMock()
        mock_svc.find_proxy.return_value = "/usr/bin/roar-proxy"
        mock_svc.get_daemon_status.return_value = None

        with (
            patch.object(proxy_cli_module, "config_get", return_value=False),
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy)
            assert result.exit_code == 0
            assert "Proxy enabled:" in result.output

    def test_proxy_enable(self, runner):
        with patch.object(proxy_cli_module, "config_set", return_value=("/tmp/config.toml", True)):
            result = runner.invoke(proxy_cli_module.proxy, ["enable"])
            assert result.exit_code == 0
            assert "enabled" in result.output.lower()
            proxy_cli_module.config_set.assert_called_once_with("proxy.enabled", "true")

    def test_proxy_disable(self, runner):
        with patch.object(proxy_cli_module, "config_set", return_value=("/tmp/config.toml", False)):
            result = runner.invoke(proxy_cli_module.proxy, ["disable"])
            assert result.exit_code == 0
            assert "disabled" in result.output.lower()
            proxy_cli_module.config_set.assert_called_once_with("proxy.enabled", "false")

    def test_proxy_start_calls_start_daemon(self, runner):
        mock_svc = MagicMock()
        mock_svc.get_daemon_status.return_value = None
        mock_svc.start_daemon.return_value = {"pid": 123, "port": 9090, "started_at": 1.0}

        with (
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy, ["start"])
            assert result.exit_code == 0
            assert "123" in result.output
            assert "9090" in result.output
            mock_svc.start_daemon.assert_called_once()

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

    def test_proxy_stop_calls_stop_daemon(self, runner):
        mock_svc = MagicMock()
        mock_svc.stop_daemon.return_value = True

        with (
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy, ["stop"])
            assert result.exit_code == 0
            assert "stopped" in result.output.lower()

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

    def test_proxy_status_subcommand(self, runner):
        mock_svc = MagicMock()
        mock_svc.find_proxy.return_value = "/usr/bin/roar-proxy"
        mock_svc.get_daemon_status.return_value = {"pid": 123, "port": 9090}

        with (
            patch.object(proxy_cli_module, "config_get", return_value=True),
            patch("roar.integrations.config.get_roar_dir", return_value="/tmp/.roar"),
            patch("roar.execution.cluster.proxy.ProxyService", return_value=mock_svc),
        ):
            result = runner.invoke(proxy_cli_module.proxy, ["status"])
            assert result.exit_code == 0
            assert "Proxy enabled: True" in result.output
            assert "/usr/bin/roar-proxy" in result.output
            assert "running" in result.output.lower()
