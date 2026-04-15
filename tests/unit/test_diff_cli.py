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
    def test_passes_refs_to_render(self, tmp_path: Path):
        runner = CliRunner()
        with patch("roar.cli.commands.diff.render_diff", return_value="output") as mock:
            result = runner.invoke(diff, ["./a.pt", "./b.pt"], obj=_ctx(tmp_path))

        assert result.exit_code == 0
        request = mock.call_args.args[0]
        assert request.ref_a == "./a.pt"
        assert request.ref_b == "./b.pt"
        assert request.output_json is False
        assert request.format == "summary"
        assert request.analyze is False

    def test_json_flag(self, tmp_path: Path):
        runner = CliRunner()
        with patch("roar.cli.commands.diff.render_diff", return_value="{}") as mock:
            result = runner.invoke(diff, ["@5", "@7", "--json"], obj=_ctx(tmp_path))

        assert result.exit_code == 0
        request = mock.call_args.args[0]
        assert request.output_json is True

    def test_format_option(self, tmp_path: Path):
        runner = CliRunner()
        with patch("roar.cli.commands.diff.render_diff", return_value="out") as mock:
            result = runner.invoke(diff, ["@5", "@7", "--format", "dag"], obj=_ctx(tmp_path))

        assert result.exit_code == 0
        assert mock.call_args.args[0].format == "dag"

    def test_analyze_flag(self, tmp_path: Path):
        runner = CliRunner()
        with patch("roar.cli.commands.diff.render_diff", return_value="out") as mock:
            result = runner.invoke(diff, ["@5", "@7", "--analyze"], obj=_ctx(tmp_path))

        assert result.exit_code == 0
        assert mock.call_args.args[0].analyze is True

    def test_depth_option(self, tmp_path: Path):
        runner = CliRunner()
        with patch("roar.cli.commands.diff.render_diff", return_value="out") as mock:
            result = runner.invoke(diff, ["@5", "@7", "--depth", "3"], obj=_ctx(tmp_path))

        assert result.exit_code == 0
        assert mock.call_args.args[0].depth == 3

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
