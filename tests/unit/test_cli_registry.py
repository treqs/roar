"""Unit tests for top-level CLI command registry behavior."""

from importlib import import_module
from unittest.mock import patch

import click
from click.testing import CliRunner

from roar.cli import LAZY_COMMANDS, cli
from roar.cli.command_registry import _COMMAND_SPECS, build_help_groups


def test_help_groups_are_built_from_command_specs() -> None:
    help_groups = dict(build_help_groups())

    assert help_groups["Start Here"] == ("init", "run", "build")
    # P1-12: `status`, `dag`, `pop`, `reset` live in a dedicated
    # Sessions group so the session-boundary primitives are
    # discoverable. `dag` moved out of "Start Here"; `pop` and
    # `reset` moved out of "Inspect Local Lineage" / "Setup and Admin"
    # respectively.
    assert help_groups["Sessions"] == ("status", "session", "dag", "pop", "reset")
    assert help_groups["Share and Publish"] == (
        "put",
        "register",
        "get",
        "label",
        "tag",
    )
    assert help_groups["Setup and Admin"] == (
        "config",
        "db",
        "filter",
        "env",
        "tracer",
        "telemetry",
        "proxy",
    )
    assert help_groups["GLaaS / TReqs Account"] == (
        "auth",
        "login",
        "logout",
        "whoami",
        "scope",
        "workflow",
    )


def test_help_does_not_list_composite_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "composite" not in result.output


def test_help_hides_internal_export_registration_package_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "export-registration-package" not in result.output
    assert "export-registration-package" in LAZY_COMMANDS


def test_help_shows_account_commands_by_default() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Start Here:" in result.output
    assert "Inspect Local Lineage:" in result.output
    assert "Share and Publish:" in result.output
    assert "Setup and Admin:" in result.output
    assert "GLaaS / TReqs Account:" in result.output
    assert "Manage GLaaS auth and SSH keys" in result.output
    assert "Store global GLaaS/TReqs auth state" in result.output
    assert "Show current GLaaS/TReqs login and repo binding" in result.output
    assert "projects" not in result.output
    assert "Generate TReqs workflow YAML from local sessions" in result.output
    assert "Track a command with provenance" in result.output
    assert "Publish artifacts and register lineage" in result.output
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


def test_cli_allows_account_commands_by_default() -> None:
    runner = CliRunner()

    login_result = runner.invoke(cli, ["login", "--help"])

    assert login_result.exit_code == 0, login_result.output
    assert "Store global GLaaS auth state." in login_result.output

    workflow_result = runner.invoke(cli, ["workflow", "--help"])

    assert workflow_result.exit_code == 0, workflow_result.output
    assert "Generate TReqs workflow YAML from local roar sessions." in workflow_result.output

    projects_result = runner.invoke(cli, ["projects", "--help"])

    assert projects_result.exit_code == 0, projects_result.output
    assert "Inspect and manage GLaaS projects." in projects_result.output


def test_tag_command_is_registered_and_reachable() -> None:
    runner = CliRunner()

    top_level_help = runner.invoke(cli, ["--help"])
    assert top_level_help.exit_code == 0, top_level_help.output
    assert "tag" in top_level_help.output
    assert "Manage hereditary compliance tags" in top_level_help.output

    tag_help = runner.invoke(cli, ["tag", "--help"])
    assert tag_help.exit_code == 0, tag_help.output
    assert "add" in tag_help.output
    assert "rm" in tag_help.output
    assert "show" in tag_help.output
    assert "history" in tag_help.output


def test_every_registered_command_spec_resolves_to_a_click_command() -> None:
    """Every CommandSpec must point at a real, importable click Command.

    A command module can exist and be fully tested in isolation while still
    being unreachable from the CLI if nobody adds its CommandSpec to
    `_COMMAND_SPECS` (this happened with `roar tag`). This walks the
    registry itself rather than the rendered --help text, so it fails loudly
    for any future command in the same situation.
    """
    for spec in _COMMAND_SPECS:
        module = import_module(spec.module_path)
        command = getattr(module, spec.attr_name)
        assert isinstance(command, click.BaseCommand), (
            f"{spec.name!r} ({spec.module_path}.{spec.attr_name}) is not a click command"
        )


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
