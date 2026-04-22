"""Localized reproduce CLI tests that are not worth exercising via subprocess."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.reproduce import reproduce


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_reproduce_cli_passes_environment_setup_flags(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.reproduce.reproduce_artifact", return_value=None) as mock_service:
        result = runner.invoke(
            reproduce,
            [
                "abc123def456",
                "--run",
                "--dpkg-any-version",
                "--pip-any-version",
                "--package-sync",
            ],
            obj=_ctx(tmp_path),
        )

    assert result.exit_code == 0
    request = mock_service.call_args.args[0]
    assert request.run_pipeline is True
    assert request.dpkg_any_version is True
    assert request.pip_any_version is True
    assert request.package_sync is True
    assert request.target_kind == "artifact"


def test_reproduce_cli_passes_lineage_target_kind(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.reproduce.reproduce_artifact", return_value=None) as mock_service:
        result = runner.invoke(
            reproduce,
            ["a" * 64, "--lineage"],
            obj=_ctx(tmp_path),
        )

    assert result.exit_code == 0
    request = mock_service.call_args.args[0]
    assert request.target_kind == "lineage"


def test_reproduce_cli_surfaces_application_errors(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch(
        "roar.cli.commands.reproduce.reproduce_artifact",
        side_effect=ValueError("Artifact not found"),
    ):
        result = runner.invoke(reproduce, ["abc123def456"], obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "Artifact not found" in result.output
