from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.query import ShowQueryRequest
from roar.cli.commands.log import log
from roar.cli.commands.show import show
from roar.cli.commands.status import status


def _ctx(tmp_path):
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.roar_dir.mkdir()
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_log_cli_exits_non_zero_without_active_session(tmp_path) -> None:
    result = CliRunner().invoke(log, obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "No active session." in result.output


def test_status_cli_exits_non_zero_without_active_session(tmp_path) -> None:
    result = CliRunner().invoke(status, obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "No active session." in result.output


def test_show_cli_exits_non_zero_for_missing_path_lookup(tmp_path) -> None:
    result = CliRunner().invoke(show, ["artifact.bin"], obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "No artifact found for path: artifact.bin" in result.output


def test_show_cli_path_selector_builds_explicit_request(tmp_path) -> None:
    ctx = _ctx(tmp_path)

    with patch("roar.cli.commands.show.render_show", return_value="ok") as render_show:
        result = CliRunner().invoke(show, ["--path", "deadbeef"], obj=ctx)

    assert result.exit_code == 0, result.output
    assert result.output == "ok\n"
    render_show.assert_called_once_with(
        ShowQueryRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, ref="deadbeef", selector="path")
    )


def test_show_cli_rejects_multiple_explicit_selectors(tmp_path) -> None:
    result = CliRunner().invoke(show, ["--path", "a", "--job", "@1"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "Specify only one of --path, --job, --artifact, or --session." in result.output


def test_show_cli_rejects_positional_ref_with_explicit_selector(tmp_path) -> None:
    result = CliRunner().invoke(show, ["--artifact", "deadbeef", "other"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "Positional REF cannot be combined" in result.output
