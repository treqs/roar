from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from roar.application.publish.put_preparation import (
    PreparedPutExecution,
    complete_delegated_put_operation,
    prepare_put_execution,
)
from roar.core.interfaces.registration import GitContext
from roar.db.context import create_database_context


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
    runtime.session_service.compute_session_hash.return_value = "local-session-hash"
    prepared_session = MagicMock(
        session_hash="session-hash",
        session_url="https://glaas/session",
        registration_session_id=None,
        registration_session_mode=None,
    )
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
        ) as prepare_session,
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
        source_hashes=prepared.source_hashes,
    )
    assert [item.path for item in prepared.resolved_sources] == [model.resolve()]
    assert prepared.source_hashes[str(model.resolve())]
    call = prepare_session.call_args.kwargs
    assert call["operation_kind"] == "put"
    assert len(call["operation_fingerprint"]) == 64


def test_prepare_put_execution_fingerprints_source_content(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    model.write_bytes(b"model-v1")
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 7}
    runtime = MagicMock()
    runtime.session_service.compute_session_hash.return_value = "local-session-hash"
    prepared_session = MagicMock(
        session_hash="session-hash",
        session_url=None,
        registration_session_id=None,
        registration_session_mode=None,
    )

    with (
        patch(
            "roar.application.publish.put_preparation.resolve_roar_git_context",
            return_value=GitContext(repo="repo", branch="main", commit="deadbeef"),
        ),
        patch(
            "roar.application.publish.put_preparation.prepare_publish_session",
            return_value=prepared_session,
        ) as prepare_session,
    ):
        prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            repo_root=tmp_path,
            sources=["model.pt"],
            destination="memory://bucket/prefix",
            git_commit="deadbeef",
            logger=MagicMock(),
        )
        first_fingerprint = prepare_session.call_args.kwargs["operation_fingerprint"]

        model.write_bytes(b"model-v2")
        prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            repo_root=tmp_path,
            sources=["model.pt"],
            destination="memory://bucket/prefix",
            git_commit="deadbeef",
            logger=MagicMock(),
        )
        second_fingerprint = prepare_session.call_args.kwargs["operation_fingerprint"]

    assert first_fingerprint != second_fingerprint


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
            return_value=MagicMock(
                session_hash="session-hash",
                session_url=None,
                registration_session_id=None,
                registration_session_mode=None,
            ),
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


def test_delegated_put_reuses_pending_retry_then_advances_identical_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_delegated_task(monkeypatch)
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    roar_dir = tmp_path / ".roar"
    runtime = MagicMock()
    runtime.session_service.compute_session_hash.return_value = "local-session-hash"
    git_context = GitContext(repo="repo", branch="main", commit="deadbeef")

    with create_database_context(roar_dir) as db_ctx:
        db_ctx.sessions.get_or_create_active()
        db_ctx.commit()
        with (
            patch(
                "roar.application.publish.put_preparation.resolve_roar_git_context",
                return_value=git_context,
            ),
            patch(
                "roar.application.publish.put_preparation.prepare_publish_session",
                return_value=MagicMock(
                    session_hash="session-hash",
                    session_url=None,
                    registration_session_id="registration-session",
                    registration_session_mode="delegated",
                    registration_session_status="active",
                ),
            ) as prepare_session,
        ):
            first = _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)
            first_fingerprint = prepare_session.call_args.kwargs["operation_fingerprint"]
            retry = _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)
            retry_fingerprint = prepare_session.call_args.kwargs["operation_fingerprint"]

            assert first.delegated_put_operation is not None
            assert retry.delegated_put_operation == first.delegated_put_operation
            assert retry_fingerprint == first_fingerprint

            complete_delegated_put_operation(db_ctx, retry.delegated_put_operation)
            second = _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)
            second_fingerprint = prepare_session.call_args.kwargs["operation_fingerprint"]

    assert second.delegated_put_operation is not None
    assert second.delegated_put_operation.ordinal == 2
    assert second_fingerprint != first_fingerprint


def test_delegated_put_rejects_changed_git_context_while_retry_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_delegated_task(monkeypatch)
    (tmp_path / "model.pt").write_bytes(b"model")
    roar_dir = tmp_path / ".roar"
    runtime = MagicMock()
    runtime.session_service.compute_session_hash.return_value = "local-session-hash"

    with create_database_context(roar_dir) as db_ctx:
        db_ctx.sessions.get_or_create_active()
        db_ctx.commit()
        with patch(
            "roar.application.publish.put_preparation.prepare_publish_session",
            return_value=MagicMock(
                session_hash="session-hash",
                session_url=None,
                registration_session_id="registration-session",
                registration_session_mode="delegated",
                registration_session_status="active",
            ),
        ):
            with patch(
                "roar.application.publish.put_preparation.resolve_roar_git_context",
                return_value=GitContext(repo="repo", branch="main", commit="first"),
            ):
                _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)
            with (
                patch(
                    "roar.application.publish.put_preparation.resolve_roar_git_context",
                    return_value=GitContext(repo="repo", branch="feature", commit="second"),
                ),
                pytest.raises(ValueError, match="different delegated put operation"),
            ):
                _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)


def test_delegated_put_rejects_changed_lineage_while_retry_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_delegated_task(monkeypatch)
    (tmp_path / "model.pt").write_bytes(b"model")
    roar_dir = tmp_path / ".roar"
    runtime = MagicMock()
    runtime.session_service.compute_session_hash.return_value = "local-session-hash"
    git_context = GitContext(repo="repo", branch="main", commit="deadbeef")

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.get_or_create_active()
        db_ctx.commit()
        with (
            patch(
                "roar.application.publish.put_preparation.resolve_roar_git_context",
                return_value=git_context,
            ),
            patch(
                "roar.application.publish.put_preparation.prepare_publish_session",
                return_value=MagicMock(
                    session_hash="session-hash",
                    session_url=None,
                    registration_session_id="registration-session",
                    registration_session_mode="delegated",
                    registration_session_status="active",
                ),
            ),
        ):
            _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)
            db_ctx.session.execute(
                text(
                    """
                    INSERT INTO jobs (timestamp, command, session_id, step_number)
                    VALUES (1, 'python upstream.py', :session_id, 1)
                    """
                ),
                {"session_id": session_id},
            )
            db_ctx.commit()

            with pytest.raises(ValueError, match="different delegated put operation"):
                _prepare_model_put(db_ctx, runtime, roar_dir, tmp_path)


def _set_delegated_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROAR_DELEGATED_JOB_ID", "job-1")
    monkeypatch.setenv("ROAR_DELEGATED_EXECUTION_ATTEMPT_ID", "attempt-1")
    monkeypatch.setenv("ROAR_DELEGATED_TASK_ID", "task-1")


def _prepare_model_put(db_ctx, runtime, roar_dir: Path, repo_root: Path) -> PreparedPutExecution:
    return prepare_put_execution(
        db_ctx=db_ctx,
        runtime=runtime,
        roar_dir=roar_dir,
        repo_root=repo_root,
        sources=["model.pt"],
        destination="memory://bucket/prefix",
        git_commit="deadbeef",
        logger=MagicMock(),
    )
