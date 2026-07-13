"""CLI wiring tests for `roar tag bind` / `roar tag unbind`.

Mocks the orchestration layer (roar.application.query.tag.tag_bind/tag_unbind)
so these exercise argument parsing, request construction, and error handling
without a real DB — the underlying mechanics are covered by
tests/unit/test_tag_service.py and tests/application/query/test_tag.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from roar.cli.commands.tag import tag as tag_group
from roar.cli.context import RoarContext


def _ctx(tmp_path: Path) -> RoarContext:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    return RoarContext(roar_dir=roar_dir, repo_root=None, cwd=tmp_path, is_interactive=False)


class TestTagBindCli:
    def test_binds_a_single_target(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch("roar.cli.commands.tag.tag_bind", return_value="Bound: model.pt") as mock_bind:
            result = runner.invoke(tag_group, ["bind", "model.pt"], obj=_ctx(tmp_path))
        assert result.exit_code == 0, result.output
        assert "Bound: model.pt" in result.output
        request = mock_bind.call_args.args[0]
        assert request.targets == ("model.pt",)

    def test_binds_multiple_targets_in_one_call(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch("roar.cli.commands.tag.tag_bind", return_value="") as mock_bind:
            result = runner.invoke(tag_group, ["bind", "one.pt", "two.pt"], obj=_ctx(tmp_path))
        assert result.exit_code == 0, result.output
        request = mock_bind.call_args.args[0]
        assert request.targets == ("one.pt", "two.pt")

    def test_requires_at_least_one_target(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(tag_group, ["bind"], obj=_ctx(tmp_path))
        assert result.exit_code != 0

    def test_value_error_becomes_click_exception(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch(
            "roar.cli.commands.tag.tag_bind", side_effect=ValueError("Artifact not found: x")
        ):
            result = runner.invoke(tag_group, ["bind", "x"], obj=_ctx(tmp_path))
        assert result.exit_code != 0
        assert "Artifact not found: x" in result.output


class TestTagUnbindCli:
    def test_unbinds_a_single_target(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch(
            "roar.cli.commands.tag.tag_unbind", return_value="Unbound: model.pt"
        ) as mock_unbind:
            result = runner.invoke(tag_group, ["unbind", "model.pt"], obj=_ctx(tmp_path))
        assert result.exit_code == 0, result.output
        assert "Unbound: model.pt" in result.output
        request = mock_unbind.call_args.args[0]
        assert request.targets == ("model.pt",)

    def test_value_error_becomes_click_exception(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch(
            "roar.cli.commands.tag.tag_unbind", side_effect=ValueError("Artifact not found: x")
        ):
            result = runner.invoke(tag_group, ["unbind", "x"], obj=_ctx(tmp_path))
        assert result.exit_code != 0
        assert "Artifact not found: x" in result.output


class TestTagGroupHelp:
    def test_bind_and_unbind_are_listed(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(tag_group, ["--help"])
        assert result.exit_code == 0
        assert "bind" in result.output
        assert "unbind" in result.output
