from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.register_preparation import (
    PreparedRegisterExecution,
    prepare_register_execution,
)
from roar.core.interfaces.registration import GitContext


def _git_context(
    *, repo: str | None = "https://github.com/test/repo", commit: str | None = "deadbeef"
) -> GitContext:
    return GitContext(repo=repo, branch="main", commit=commit)


def test_prepare_register_execution_builds_session_git_and_tag_plan(tmp_path: Path) -> None:
    runtime = MagicMock()
    logger = MagicMock()
    prepared_session = MagicMock(session_hash="session-hash", session_url="https://glaas/session")
    git_context = _git_context()
    git_state = MagicMock(repo_root=tmp_path)

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_publish_git_context",
            return_value=git_context,
        ),
        patch(
            "roar.application.publish.register_preparation.ensure_clean_publish_repo",
            return_value=git_state,
        ) as ensure_clean,
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            return_value=prepared_session,
        ) as prepare_session,
        patch("roar.application.publish.register_preparation.config_get", return_value=True),
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
    prepare_session.assert_called_once_with(
        glaas_client=runtime.glaas_client,
        session_service=runtime.session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=git_context,
        logger=logger,
        register_with_glaas=True,
        configured_error="GLaaS not configured. Run 'roar config set glaas.url <url>' first.",
        session_hash_override=None,
    )


def test_prepare_register_execution_skips_git_tagging_and_glaas_on_dry_run(tmp_path: Path) -> None:
    runtime = MagicMock()
    prepared_session = MagicMock(session_hash="session-hash", session_url=None)
    git_context = _git_context()

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_publish_git_context",
            return_value=git_context,
        ),
        patch(
            "roar.application.publish.register_preparation.ensure_clean_publish_repo"
        ) as ensure_clean,
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            return_value=prepared_session,
        ) as prepare_session,
        patch("roar.application.publish.register_preparation.config_get", return_value=True),
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
    ensure_clean.assert_not_called()
    assert prepare_session.call_args.kwargs["register_with_glaas"] is False


def test_prepare_register_execution_propagates_dirty_repo_error(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch(
            "roar.application.publish.register_preparation.resolve_publish_git_context",
            return_value=_git_context(),
        ),
        patch(
            "roar.application.publish.register_preparation.ensure_clean_publish_repo",
            side_effect=ValueError("dirty repo"),
        ),
        patch("roar.application.publish.register_preparation.config_get", return_value=True),
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
            "roar.application.publish.register_preparation.resolve_publish_git_context",
            return_value=_git_context(),
        ),
        patch(
            "roar.application.publish.register_preparation.prepare_publish_session",
            side_effect=ValueError("GLaaS not configured"),
        ),
        patch("roar.application.publish.register_preparation.config_get", return_value=False),
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
