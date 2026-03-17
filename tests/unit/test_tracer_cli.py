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

    def test_set_default_preload_via_alias(self):
        runner = CliRunner()
        with patch.object(
            tracer_cli_module, "config_set", return_value=("/tmp/config.toml", "preload")
        ) as mock_set:
            result = runner.invoke(tracer_cli_module.tracer, ["preload"])

        assert result.exit_code == 0
        mock_set.assert_called_once_with("tracer.default", "preload")
        assert "Default tracer set to: preload" in result.output
