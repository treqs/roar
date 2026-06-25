"""Unit tests for the ``roar session`` command group."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.query.status import StatusQueryError
from roar.cli.commands.session import session


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_session_hash_prints_only_the_hash(tmp_path: Path) -> None:
    """`roar session hash` prints just the hash so it can feed $(...) substitution."""
    runner = CliRunner()
    digest = "d" * 64
    with patch(
        "roar.cli.commands.session.compute_active_session_hash",
        return_value=digest,
    ):
        result = runner.invoke(session, ["hash"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert result.output.strip() == digest


def test_session_hash_errors_cleanly_without_active_session(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch(
        "roar.cli.commands.session.compute_active_session_hash",
        side_effect=StatusQueryError(
            "No active session. Run 'roar run' to create a session first."
        ),
    ):
        result = runner.invoke(session, ["hash"], obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "No active session" in result.output
