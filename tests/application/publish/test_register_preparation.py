from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.register_preparation import (
    prepare_register_execution,
)
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext


def _git_context(
    *, repo: str | None = "https://github.com/test/repo", commit: str | None = "deadbeef"
) -> GitContext:
    return GitContext(repo=repo, branch="main", commit=commit)


def _lineage() -> LineageData:
    return LineageData(jobs=[{"job_uid": "job-1", "step_number": 1, "_inputs": [], "_outputs": []}])


def test_prepare_register_execution_skips_git_tagging_and_glaas_on_dry_run(tmp_path: Path) -> None:
    runtime = MagicMock()
    prepared_session = MagicMock(
        session_hash="session-hash",
        session_url=None,
        registration_session_id=None,
        registration_session_mode=None,
    )
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
    # git.remote is read to resolve the repo URL (the only config read in prep,
    # even on a dry run); no other config is consulted.
    config_get.assert_called_once_with("git.remote")
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
