"""Unit tests for top-level CLI command registry behavior."""

from click.testing import CliRunner

from roar.cli import LAZY_COMMANDS, cli


def test_composite_command_removed_from_lazy_registry() -> None:
    assert "composite" not in LAZY_COMMANDS


def test_help_does_not_list_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "composite" not in result.output


def test_cli_rejects_removed_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["composite"])

    assert result.exit_code == 2
    assert "No such command 'composite'" in result.output
