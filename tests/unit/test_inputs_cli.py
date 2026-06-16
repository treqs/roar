"""CLI tests for the inputs command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.inputs import inputs


def _ctx(tmp_path):
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.roar_dir.mkdir()
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_inputs_cli_requires_ref(tmp_path) -> None:
    result = CliRunner().invoke(inputs, [], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "target reference is required" in result.output


def test_inputs_cli_rejects_direct_and_all_together(tmp_path) -> None:
    result = CliRunner().invoke(inputs, ["--direct", "--all", "model.pkl"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "--direct and --all are mutually exclusive" in result.output


def test_inputs_cli_rejects_multiple_explicit_selectors(tmp_path) -> None:
    result = CliRunner().invoke(inputs, ["--path", "a", "--job", "@1"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "Specify only one of" in result.output


def test_inputs_cli_rejects_positional_ref_with_explicit_selector(tmp_path) -> None:
    result = CliRunner().invoke(inputs, ["--artifact", "deadbeef", "other"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "Positional REF cannot be combined" in result.output


def test_inputs_cli_surfaces_query_error_as_click_exception(tmp_path) -> None:
    from roar.application.query.inputs import InputsQueryError

    with patch(
        "roar.cli.commands.inputs.render_inputs",
        side_effect=InputsQueryError("not tracked"),
    ):
        result = CliRunner().invoke(inputs, ["missing.bin"], obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "not tracked" in result.output
