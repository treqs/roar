"""Unit tests for top-level CLI command registry behavior."""

from unittest.mock import patch

from click.testing import CliRunner

from roar.cli import LAZY_COMMANDS, cli


def test_composite_command_removed_from_lazy_registry() -> None:
    assert "composite" not in LAZY_COMMANDS


def test_help_does_not_list_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "composite" not in result.output


def test_help_groups_commands_by_workflow() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Start Here:" in result.output
    assert "Inspect Local Lineage:" in result.output
    assert "Share and Publish:" in result.output
    assert "Setup and Admin:" in result.output
    assert "GLaaS / TReqs Account:" in result.output
    assert result.output.index("Setup and Admin:") < result.output.index("GLaaS / TReqs Account:") < result.output.index("Other Commands:")
    assert "Track a command with provenance" in result.output
    assert "Publish artifacts and register lineage" in result.output


def test_cli_rejects_removed_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["composite"])

    assert result.exit_code == 2
    assert "No such command 'composite'" in result.output


def test_subcommand_help_reports_import_errors_cleanly() -> None:
    runner = CliRunner()
    missing = ModuleNotFoundError("No module named 'pydantic'")
    missing.name = "pydantic"

    with patch("roar.cli.import_module", side_effect=missing):
        result = runner.invoke(cli, ["run", "--help"])

    assert result.exit_code != 0
    assert "Failed to load 'run'" in result.output
    assert "pydantic" in result.output
    assert "Traceback" not in result.output
