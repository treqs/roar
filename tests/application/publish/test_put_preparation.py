from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.put_preparation import PreparedPutExecution, prepare_put_execution
from roar.core.interfaces.registration import GitContext


def test_prepare_put_execution_requires_active_session(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = None
    runtime = MagicMock()

    with pytest.raises(ValueError, match="No active session"):
        prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            repo_root=tmp_path,
            sources=["model.pt"],
            destination="memory://bucket/prefix",
            git_commit=None,
            logger=MagicMock(),
        )


def test_prepare_put_execution_builds_session_git_and_source_plan(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")

    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 7}
    runtime = MagicMock()
    prepared_session = MagicMock(session_hash="session-hash", session_url="https://glaas/session")
    logger = MagicMock()
    git_context = GitContext(repo="https://github.com/test/repo", branch="main", commit="deadbeef")

    with (
        patch(
            "roar.application.publish.put_preparation.resolve_roar_git_context",
            return_value=git_context,
        ),
        patch(
            "roar.application.publish.put_preparation.prepare_publish_session",
            return_value=prepared_session,
        ),
        patch(
            "roar.application.publish.put_preparation.infer_publish_dataset_identifiers",
            return_value=[],
        ),
        patch(
            "roar.application.publish.put_preparation.detect_additional_publish_composite_roots",
            return_value={},
        ),
    ):
        prepared = prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            repo_root=tmp_path,
            sources=["model.pt"],
            destination="memory://bucket/prefix",
            git_commit="deadbeef",
            logger=logger,
        )

    assert prepared == PreparedPutExecution(
        glaas_client=runtime.glaas_client,
        session_id=7,
        session_hash="session-hash",
        session_url="https://glaas/session",
        git_context=git_context,
        resolved_sources=prepared.resolved_sources,
        destination_type="memory",
        composite_source_type=None,
    )
    assert [item.path for item in prepared.resolved_sources] == [model.resolve()]


def test_prepare_put_execution_propagates_missing_source(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 7}
    runtime = MagicMock()

    with (
        patch(
            "roar.application.publish.put_preparation.resolve_roar_git_context",
            return_value=GitContext(repo="repo", branch="main", commit="deadbeef"),
        ),
        patch(
            "roar.application.publish.put_preparation.prepare_publish_session",
            return_value=MagicMock(session_hash="session-hash", session_url=None),
        ),
        pytest.raises(FileNotFoundError, match=r"Source not found: missing.pt"),
    ):
        prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            repo_root=tmp_path,
            sources=["missing.pt"],
            destination="memory://bucket/prefix",
            git_commit="deadbeef",
            logger=MagicMock(),
        )
