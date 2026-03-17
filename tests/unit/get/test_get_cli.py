"""Unit tests for the thin get CLI wrapper."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.get import get


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.repo_root = tmp_path
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_source_required(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(get, [], obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Missing argument" in result.output or "Error" in result.output


def test_cli_surfaces_application_errors(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.get.get_artifacts", side_effect=ValueError("Unsupported")):
        result = runner.invoke(get, ["ftp://server/file.pt"], obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Unsupported" in result.output
