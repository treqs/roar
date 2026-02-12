"""Tests for config CLI behavior after tracer command deprecation removal."""

import importlib

from click.testing import CliRunner

config_cli_module = importlib.import_module("roar.cli.commands.config")


class TestConfigCli:
    def test_tracer_subcommand_is_not_available(self):
        runner = CliRunner()
        result = runner.invoke(config_cli_module.config, ["tracer"])

        assert result.exit_code != 0
        assert "No such command 'tracer'" in result.output

    def test_setting_legacy_tracer_mode_key_fails(self):
        runner = CliRunner()
        result = runner.invoke(config_cli_module.config, ["set", "tracer.mode", "auto"])

        assert result.exit_code != 0
        assert "Unknown config key: tracer.mode" in result.output
