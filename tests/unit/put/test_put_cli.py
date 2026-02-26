"""Unit tests for put CLI output behavior."""

from __future__ import annotations

import importlib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.put import put
from roar.services.put.service import PutResult

put_module = importlib.import_module("roar.cli.commands.put")


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
        patch.object(put_module, "bootstrap"),
        patch.object(put_module, "create_database_context", return_value=nullcontext(db_ctx)),
        patch.object(put_module, "PutService", return_value=service),
        patch.object(put_module, "GitOperations", return_value=_make_git_ops()),
        patch.object(put_module, "config_get", return_value="https://glaas.example"),
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
        patch.object(put_module, "bootstrap"),
        patch.object(put_module, "create_database_context", return_value=nullcontext(db_ctx)),
        patch.object(put_module, "PutService", return_value=service),
        patch.object(put_module, "GitOperations", return_value=_make_git_ops()),
        patch.object(put_module, "config_get", return_value="https://glaas.example"),
    ):
        result = runner.invoke(
            put,
            ["--no-tag", "-m", "publish", "artifact.bin", "memory://bucket/prefix"],
            obj=ctx,
        )

    assert result.exit_code == 0, result.output
    assert "https://glaas.example/dag/service_hash_only" in result.output
    assert "https://glaas.example/dag/wrong_local_hash" not in result.output


def test_put_prints_registered_composite_summary(tmp_path: Path) -> None:
    runner = CliRunner()
    ctx = _make_ctx(tmp_path)
    db_ctx = _make_db_ctx()
    dataset_root = tmp_path / "dataset"
    composite_hash = "a" * 64

    service = MagicMock()
    service.put.return_value = PutResult(
        success=True,
        job_id=10,
        uploaded_files=[
            {
                "local_path": str(tmp_path / "artifact.bin"),
                "remote_url": "memory://bucket/prefix/artifact.bin",
            }
        ],
        composites_registered=[
            {
                "root_path": str(dataset_root),
                "hash": composite_hash,
                "component_count_stored": 2,
                "component_count_total": 4,
                "artifact_id": "comp-123",
            }
        ],
        session_hash="session_hash",
        session_url="https://glaas.example/dag/session_hash",
    )

    with (
        patch.object(put_module, "bootstrap"),
        patch.object(put_module, "create_database_context", return_value=nullcontext(db_ctx)),
        patch.object(put_module, "PutService", return_value=service),
        patch.object(put_module, "GitOperations", return_value=_make_git_ops()),
        patch.object(put_module, "config_get", return_value="https://glaas.example"),
    ):
        result = runner.invoke(
            put,
            ["--no-tag", "-m", "publish", "artifact.bin", "memory://bucket/prefix"],
            obj=ctx,
        )

    assert result.exit_code == 0, result.output
    assert "Registered 1 composite artifact(s):" in result.output
    assert (
        f"{dataset_root} -> {composite_hash[:12]}... (2/4 components stored) id=comp-123"
        in result.output
    )


def test_put_warns_when_local_composite_persistence_fails(tmp_path: Path) -> None:
    runner = CliRunner()
    ctx = _make_ctx(tmp_path)
    db_ctx = _make_db_ctx()
    dataset_root = tmp_path / "dataset"

    service = MagicMock()
    service.put.return_value = PutResult(
        success=True,
        job_id=11,
        uploaded_files=[
            {
                "local_path": str(tmp_path / "artifact.bin"),
                "remote_url": "memory://bucket/prefix/artifact.bin",
            }
        ],
        composites_registered=[
            {
                "root_path": str(dataset_root),
                "hash": "b" * 64,
                "component_count_stored": 1,
                "component_count_total": 1,
                "registered": True,
                "local_persisted": False,
                "local_error": "sqlite busy",
            }
        ],
        session_hash="session_hash",
        session_url="https://glaas.example/dag/session_hash",
    )

    with (
        patch.object(put_module, "bootstrap"),
        patch.object(put_module, "create_database_context", return_value=nullcontext(db_ctx)),
        patch.object(put_module, "PutService", return_value=service),
        patch.object(put_module, "GitOperations", return_value=_make_git_ops()),
        patch.object(put_module, "config_get", return_value="https://glaas.example"),
    ):
        result = runner.invoke(
            put,
            ["--no-tag", "-m", "publish", "artifact.bin", "memory://bucket/prefix"],
            obj=ctx,
        )

    assert result.exit_code == 0, result.output
    assert "Warning: local composite metadata was not persisted" in result.output
    assert "sqlite busy" in result.output
