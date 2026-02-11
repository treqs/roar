"""Unit tests for put CLI output behavior."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.put import put
from roar.services.put.service import PutResult


def _make_ctx(tmp_path: Path) -> SimpleNamespace:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    return SimpleNamespace(
        roar_dir=roar_dir,
        repo_root=tmp_path,
        cwd=tmp_path,
        is_initialized=True,
    )


def _make_db_ctx(active_hash: str = "local_session_hash") -> MagicMock:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 1, "hash": active_hash}
    return db_ctx


def _make_git_ops() -> MagicMock:
    git_ops = MagicMock()
    git_ops.has_uncommitted_changes.return_value = False
    git_ops.get_current_commit.return_value = "deadbeef"
    return git_ops


def test_put_uses_service_session_url_for_dag_link(tmp_path: Path) -> None:
    runner = CliRunner()
    ctx = _make_ctx(tmp_path)
    db_ctx = _make_db_ctx(active_hash="wrong_local_hash")

    service = MagicMock()
    service.put.return_value = PutResult(
        success=True,
        job_id=7,
        uploaded_files=[
            {
                "local_path": str(tmp_path / "model.pt"),
                "remote_url": "memory://bucket/prefix/model.pt",
            }
        ],
        session_hash="correct_hash",
        session_url="https://glaas.example/dag/correct_hash",
    )

    with (
        patch("roar.cli.commands.put.bootstrap"),
        patch("roar.cli.commands.put.create_database_context", return_value=nullcontext(db_ctx)),
        patch("roar.cli.commands.put.PutService", return_value=service),
        patch("roar.cli.commands.put.GitOperations", return_value=_make_git_ops()),
        patch("roar.cli.commands.put.config_get", return_value="https://glaas.example"),
    ):
        result = runner.invoke(
            put,
            ["--no-tag", "-m", "publish", "model.pt", "memory://bucket/prefix"],
            obj=ctx,
        )

    assert result.exit_code == 0, result.output
    assert "https://glaas.example/dag/correct_hash" in result.output
    assert "https://glaas.example/dag/wrong_local_hash" not in result.output


def test_put_falls_back_to_web_url_plus_service_session_hash(tmp_path: Path) -> None:
    runner = CliRunner()
    ctx = _make_ctx(tmp_path)
    db_ctx = _make_db_ctx(active_hash="wrong_local_hash")

    service = MagicMock()
    service.put.return_value = PutResult(
        success=True,
        job_id=8,
        uploaded_files=[
            {
                "local_path": str(tmp_path / "artifact.bin"),
                "remote_url": "memory://bucket/prefix/artifact.bin",
            }
        ],
        session_hash="service_hash_only",
        session_url=None,
    )

    with (
        patch("roar.cli.commands.put.bootstrap"),
        patch("roar.cli.commands.put.create_database_context", return_value=nullcontext(db_ctx)),
        patch("roar.cli.commands.put.PutService", return_value=service),
        patch("roar.cli.commands.put.GitOperations", return_value=_make_git_ops()),
        patch("roar.cli.commands.put.config_get", return_value="https://glaas.example"),
    ):
        result = runner.invoke(
            put,
            ["--no-tag", "-m", "publish", "artifact.bin", "memory://bucket/prefix"],
            obj=ctx,
        )

    assert result.exit_code == 0, result.output
    assert "https://glaas.example/dag/service_hash_only" in result.output
    assert "https://glaas.example/dag/wrong_local_hash" not in result.output
