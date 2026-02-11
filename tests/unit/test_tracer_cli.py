"""Tests for the `roar tracer` CLI command group."""

import importlib
from unittest.mock import patch

from click.testing import CliRunner

tracer_cli_module = importlib.import_module("roar.cli.commands.tracer")


class TestTracerCli:
    def test_set_default_writes_tracer_default_key(self):
        runner = CliRunner()
        with patch.object(
            tracer_cli_module, "config_set", return_value=("/tmp/config.toml", "ebpf")
        ) as mock_set:
            result = runner.invoke(tracer_cli_module.tracer, ["set-default", "ebpf"])

        assert result.exit_code == 0
        mock_set.assert_called_once_with("tracer.default", "ebpf")
        assert "Default tracer set to: ebpf" in result.output

    def test_check_ptrace_fails_when_binary_missing(self):
        runner = CliRunner()
        with patch.object(tracer_cli_module, "_find_ptrace_tracer", return_value=None):
            result = runner.invoke(tracer_cli_module.tracer, ["check", "--backend", "ptrace"])

        assert result.exit_code == 1
        assert "Tracer check failed for 'ptrace'" in result.output

    def test_check_ptrace_passes_when_binary_present(self):
        runner = CliRunner()
        with patch.object(tracer_cli_module, "_find_ptrace_tracer", return_value="/usr/bin/roar-tracer"):
            result = runner.invoke(tracer_cli_module.tracer, ["check", "--backend", "ptrace"])

        assert result.exit_code == 0
        assert "Tracer check passed for 'ptrace'" in result.output

    def test_status_shows_default_and_fallback(self):
        runner = CliRunner()
        with (
            patch.object(tracer_cli_module, "config_get") as mock_get,
            patch.object(tracer_cli_module, "_find_ptrace_tracer", return_value=None),
            patch.object(tracer_cli_module, "_find_ebpf_tracer", return_value=None),
            patch.object(tracer_cli_module, "_find_roard", return_value=None),
            patch.object(tracer_cli_module, "_get_perf_event_paranoid", return_value=2),
        ):
            mock_get.side_effect = lambda key: {
                "tracer.default": "auto",
                "tracer.fallback_enabled": True,
                "proxy.enabled": False,
            }.get(key)
            result = runner.invoke(tracer_cli_module.tracer, ["status"])

        assert result.exit_code == 0
        assert "Default tracer: auto" in result.output
        assert "Fallback enabled: True" in result.output
        assert "Proxy enabled: False" in result.output
