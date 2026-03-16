"""Unit tests for the thin reproduce CLI wrapper."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.reproduce.requests import ReproduceRequest
from roar.cli.commands.reproduce import reproduce


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_reproduce_cli_builds_application_request(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.reproduce.reproduce_artifact", return_value=None) as mock_service:
        result = runner.invoke(
            reproduce,
            [
                "abc123def456",
                "--run",
                "-y",
                "--dpkg-any-version",
                "--pip-any-version",
                "--package-sync",
                "--list-requirements",
                "--out",
                "dag.json",
            ],
            obj=_ctx(tmp_path),
        )

    assert result.exit_code == 0
    request = mock_service.call_args.args[0]
    assert isinstance(request, ReproduceRequest)
    assert request.hash_prefix == "abc123def456"
    assert request.roar_dir == tmp_path / ".roar"
    assert request.cwd == tmp_path
    assert request.run_pipeline is True
    assert request.auto_confirm is True
    assert request.dpkg_any_version is True
    assert request.pip_any_version is True
    assert request.package_sync is True
    assert request.list_requirements is True
    assert request.out_path == "dag.json"


def test_reproduce_cli_surfaces_application_errors(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch(
        "roar.cli.commands.reproduce.reproduce_artifact",
        side_effect=ValueError("Artifact not found"),
    ):
        result = runner.invoke(reproduce, ["abc123def456"], obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "Artifact not found" in result.output
