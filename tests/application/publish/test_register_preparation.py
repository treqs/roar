from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.register_preparation import (
    PreparedRegisterExecution,
    prepare_register_execution,
)
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext
from roar.publish_auth import PublishAuthContext


def _git_context(
    *, repo: str | None = "https://github.com/test/repo", commit: str | None = "deadbeef"
) -> GitContext:
    return GitContext(repo=repo, branch="main", commit=commit)


def _lineage() -> LineageData:
    return LineageData(jobs=[{"job_uid": "job-1", "step_number": 1, "_inputs": [], "_outputs": []}])


def test_prepare_register_execution_builds_session_git_and_tag_plan(tmp_path: Path) -> None:
    runtime = MagicMock()
    runtime.glaas_client.publish_auth = PublishAuthContext(
        access_token=None,
        scope_request=None,
        auth_provider=None,
        user_sub=None,
        db_user_id=None,
    )
    logger = MagicMock()
    prepared_session = MagicMock(session_hash="session-hash", session_url="https://glaas/session")
    git_context = _git_context()
    git_state = MagicMock(repo_root=tmp_path)

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_roar_git_context",
            return_value=git_context,
        ),
        patch(
            "roar.application.publish.register_preparation.ensure_clean_git_repo",
            return_value=git_state,
        ) as ensure_clean,
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            return_value=prepared_session,
        ) as prepare_session,
        patch("roar.integrations.config.config_get", return_value=True),
    ):
        prepared = prepare_register_execution(
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            session_id=7,
            dry_run=False,
            session_hash_override=None,
            logger=logger,
        )

    assert prepared == PreparedRegisterExecution(
        git_context=git_context,
        session_id=7,
        session_hash="session-hash",
        session_url="https://glaas/session",
        git_tag_name="roar/deadbeef",
        git_tag_repo_root=tmp_path,
    )
    ensure_clean.assert_called_once_with(
        tmp_path,
        error_message="Cannot register with uncommitted changes. Commit your changes first.",
    )
    prepare_session.assert_called_once()
    prepare_kwargs = prepare_session.call_args.kwargs
    assert prepare_kwargs["remote_registry"].client is runtime.glaas_client
    assert prepare_kwargs["remote_registry"].session_service is runtime.session_service
    assert prepare_kwargs["roar_dir"] == tmp_path / ".roar"
    assert prepare_kwargs["session_id"] == 7
    assert prepare_kwargs["git_context"] == git_context
    assert prepare_kwargs["logger"] is logger
    assert prepare_kwargs["register_with_glaas"] is True
    assert (
        prepare_kwargs["configured_error"]
        == "GLaaS not configured. Run 'roar config set glaas.url <url>' first."
    )
    assert prepare_kwargs["session_hash_override"] is None
    assert prepare_kwargs["lineage"] is None
    assert prepare_kwargs["creator_identity"] == "anonymous"


def test_prepare_register_execution_passes_lineage_and_creator_identity_to_session_preparation(
    tmp_path: Path,
) -> None:
    runtime = MagicMock()
    runtime.glaas_client.publish_auth = PublishAuthContext(
        access_token="token-123",
        scope_request=None,
        auth_provider="treqs-device",
        user_sub="treqs-sub-123",
        db_user_id="user-123",
    )
    prepared_session = MagicMock(session_hash="session-hash", session_url=None)

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_roar_git_context",
            return_value=_git_context(),
        ),
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            return_value=prepared_session,
        ) as prepare_session,
        patch("roar.integrations.config.config_get", return_value=False),
    ):
        prepare_register_execution(
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            session_id=7,
            dry_run=False,
            session_hash_override=None,
            logger=MagicMock(),
            lineage=_lineage(),
        )

    assert prepare_session.call_args.kwargs["lineage"] == _lineage()
    assert prepare_session.call_args.kwargs["creator_identity"] == "treqs:user:treqs-sub-123"


def test_prepare_register_execution_skips_git_tagging_and_glaas_on_dry_run(tmp_path: Path) -> None:
    runtime = MagicMock()
    prepared_session = MagicMock(session_hash="session-hash", session_url=None)
    git_context = _git_context()

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_roar_git_context",
            return_value=git_context,
        ),
        patch(
            "roar.application.publish.register_preparation.ensure_clean_git_repo"
        ) as ensure_clean,
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            return_value=prepared_session,
        ) as prepare_session,
        patch("roar.integrations.config.config_get") as config_get,
    ):
        prepared = prepare_register_execution(
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            session_id=7,
            dry_run=True,
            session_hash_override=None,
            logger=MagicMock(),
        )

    assert prepared.git_tag_name is None
    assert prepared.git_tag_repo_root is None
    config_get.assert_not_called()
    ensure_clean.assert_not_called()
    assert prepare_session.call_args.kwargs["register_with_glaas"] is False


def test_prepare_register_execution_propagates_dirty_repo_error(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_roar_git_context",
            return_value=_git_context(),
        ),
        patch(
            "roar.application.publish.register_preparation.ensure_clean_git_repo",
            side_effect=ValueError("dirty repo"),
        ),
        patch("roar.integrations.config.config_get", return_value=True),
        pytest.raises(ValueError, match="dirty repo"),
    ):
        prepare_register_execution(
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            session_id=7,
            dry_run=False,
            session_hash_override=None,
            logger=MagicMock(),
        )


def test_prepare_register_execution_propagates_session_preparation_error(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_roar_git_context",
            return_value=_git_context(),
        ),
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            side_effect=ValueError("GLaaS not configured"),
        ),
        patch("roar.integrations.config.config_get", return_value=False),
        pytest.raises(ValueError, match="GLaaS not configured"),
    ):
        prepare_register_execution(
            runtime=runtime,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            session_id=7,
            dry_run=False,
            session_hash_override=None,
            logger=MagicMock(),
        )
