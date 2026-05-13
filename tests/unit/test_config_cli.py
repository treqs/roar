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

    def test_project_telemetry_enabled_can_be_disabled(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                config_cli_module.config,
                ["set", "telemetry.enabled", "false"],
            )

        assert result.exit_code == 0, result.output
        assert "Set telemetry.enabled = False" in result.output

    def test_project_telemetry_endpoint_can_be_set(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                config_cli_module.config,
                [
                    "set",
                    "telemetry.endpoint",
                    "https://api.dev.glaas.ai/api/v1/telemetry/roar",
                ],
            )

        assert result.exit_code == 0, result.output
        assert (
            "Set telemetry.endpoint = https://api.dev.glaas.ai/api/v1/telemetry/roar"
            in result.output
        )
