from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.get.requests import GetRequest
from roar.application.get.service import get_artifacts
from roar.services.get.service import GetResult


def _request(tmp_path: Path, **overrides) -> GetRequest:
    return GetRequest(
        source=overrides.pop("source", "s3://bucket/model.pt"),
        destination=overrides.pop("destination", tmp_path / "model.pt"),
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        repo_root=overrides.pop("repo_root", tmp_path),
        message=overrides.pop("message", None),
        expected_hash=overrides.pop("expected_hash", None),
        dry_run=overrides.pop("dry_run", False),
        force=overrides.pop("force", False),
        tag=overrides.pop("tag", False),
        **overrides,
    )


def test_get_artifacts_resolves_backend_and_executes_service(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False)
    backend = MagicMock()
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    db_ctx.sessions.get_active.return_value = {"id": 1}
    service = MagicMock()
    service.get.return_value = GetResult(success=True, downloaded_files=[])

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=backend),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        response = get_artifacts(_request(tmp_path))

    assert response.result.success is True
    db_ctx.sessions.get_active.assert_called_once()
    service.get.assert_called_once()


def test_get_artifacts_skips_active_session_check_on_dry_run(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False)
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetResult(success=True, dry_run=True, would_download=[])

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
    ):
        response = get_artifacts(_request(tmp_path, dry_run=True))

    assert response.result.dry_run is True
    db_ctx.sessions.get_active.assert_not_called()


def test_get_artifacts_requires_active_session_for_real_downloads(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False)
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    db_ctx.sessions.get_active.return_value = None

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        with pytest.raises(ValueError, match="No active session"):
            get_artifacts(_request(tmp_path))


def test_get_artifacts_creates_roar_git_tag(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False)
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    db_ctx.sessions.get_active.return_value = {"id": 1}
    service = MagicMock()
    service.get.return_value = GetResult(success=True, downloaded_files=[])

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
        patch(
            "roar.application.get.service.create_roar_git_tag",
            return_value=(True, None),
        ) as create_tag,
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        response = get_artifacts(_request(tmp_path, tag=True))

    assert response.git_tag == "roar/deadbeef"
    create_tag.assert_called_once_with(tmp_path, "roar/deadbeef")
