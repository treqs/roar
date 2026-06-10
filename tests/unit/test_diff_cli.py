"""Unit tests for the diff CLI command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.query.diff import DiffError
from roar.cli.commands.diff import diff


def _ctx(tmp_path: Path) -> MagicMock:
    """Create a mock RoarContext."""
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


class TestDiffCli:
    def test_diff_error_surfaces_as_click_exception(self, tmp_path: Path):
        runner = CliRunner()
        with patch(
            "roar.cli.commands.diff.render_diff",
            side_effect=DiffError("Artifact not found: xyz"),
        ):
            result = runner.invoke(diff, ["xyz", "abc"], obj=_ctx(tmp_path))

        assert result.exit_code != 0
        assert "Artifact not found" in result.output

    def test_missing_ref_arg(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(diff, ["@5"], obj=_ctx(tmp_path))
        assert result.exit_code != 0

    def test_invalid_format_rejected(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(diff, ["@5", "@7", "--format", "invalid"], obj=_ctx(tmp_path))
        assert result.exit_code != 0

    def test_help_does_not_reference_glaas_prefix(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(diff, ["--help"], obj=_ctx(tmp_path))

        assert result.exit_code == 0, result.output
        assert "glaas:" not in result.output
        assert "deadbeefcafebabe" in result.output
