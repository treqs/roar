"""Unit tests for top-level CLI command registry behavior."""

from unittest.mock import patch

from click.testing import CliRunner

from roar.cli import EXPERIMENTAL_ACCOUNT_COMMANDS_FLAG, LAZY_COMMANDS, cli


def test_composite_command_removed_from_lazy_registry() -> None:
    assert "composite" not in LAZY_COMMANDS


def test_help_does_not_list_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "composite" not in result.output


def test_help_hides_experimental_account_commands_by_default() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Start Here:" in result.output
    assert "Inspect Local Lineage:" in result.output
    assert "Share and Publish:" in result.output
    assert "Setup and Admin:" in result.output
    assert "GLaaS / TReqs Account:" not in result.output
    assert "Store global GLaaS/TReqs auth state" not in result.output
    assert "Track a command with provenance" in result.output
    assert "Publish artifacts and register lineage" in result.output


def test_help_shows_experimental_account_commands_with_feature_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--help"],
        env={EXPERIMENTAL_ACCOUNT_COMMANDS_FLAG: "1"},
    )

    assert result.exit_code == 0, result.output
    assert "GLaaS / TReqs Account:" in result.output
    assert "Store global GLaaS/TReqs auth state" in result.output
    assert "Show current GLaaS/TReqs login and repo binding" in result.output
    assert (
        result.output.index("Setup and Admin:")
        < result.output.index("GLaaS / TReqs Account:")
        < result.output.index("Other Commands:")
    )


def test_cli_rejects_removed_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["composite"])

    assert result.exit_code == 2
    assert "No such command 'composite'" in result.output


def test_cli_rejects_experimental_account_commands_by_default() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["login"])

    assert result.exit_code == 2
    assert "No such command 'login'" in result.output


def test_cli_allows_experimental_account_commands_with_feature_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["login", "--help"],
        env={EXPERIMENTAL_ACCOUNT_COMMANDS_FLAG: "1"},
    )

    assert result.exit_code == 0, result.output
    assert "Store global GLaaS auth state." in result.output


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
