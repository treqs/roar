from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.get.requests import GetRequest
from roar.application.get.results import GetDownloadedFile, GetDryRunItem, GetResponse
from roar.application.get.service import get_artifacts
from roar.application.get.transfer import GetTransferResult


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
    parsed_source = MagicMock(is_prefix=False, scheme="s3")
    backend = MagicMock()
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetTransferResult(
        success=True,
        downloaded_files=[
            GetDownloadedFile(
                remote_url="s3://bucket/model.pt",
                local_path=str(tmp_path / "model.pt"),
                hash="abc123",
                size=5,
            )
        ],
    )
    recorder = MagicMock()
    recorder.record.return_value = (42, "job-uid-1")

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=backend),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
        patch("roar.application.get.service.LocalJobRecorder", return_value=recorder),
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        response = get_artifacts(_request(tmp_path))

    assert response.success is True
    service.get.assert_called_once()
    recorder.record.assert_called_once()
    assert response.job_id == 42
    assert response.job_uid == "job-uid-1"


def test_get_artifacts_builds_get_metadata_for_shared_recorder(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False, scheme="s3")
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetTransferResult(
        success=True,
        downloaded_files=[
            GetDownloadedFile(
                remote_url="s3://bucket/model.pt",
                local_path=str(tmp_path / "model.pt"),
                hash="abc123",
                size=5,
            )
        ],
    )
    recorder = MagicMock()
    recorder.record.return_value = (42, "job-uid-1")

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
        patch("roar.application.get.service.LocalJobRecorder", return_value=recorder),
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        get_artifacts(_request(tmp_path, message="download model"))

    metadata = json.loads(recorder.record.call_args.kwargs["metadata"])
    assert metadata["get"]["source"] == "s3://bucket/model.pt"
    assert metadata["get"]["source_type"] == "s3"
    assert metadata["get"]["message"] == "download model"
    assert metadata["get"]["git_commit"] == "deadbeef"


def test_get_artifacts_skips_active_session_check_on_dry_run(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False, scheme="s3")
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetTransferResult(
        success=True,
        dry_run=True,
        would_download=[GetDryRunItem(remote_url="s3://bucket/model.pt", local_path="model.pt")],
    )

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
        patch("roar.application.get.service.LocalJobRecorder") as recorder_cls,
    ):
        response = get_artifacts(_request(tmp_path, dry_run=True))

    assert response.dry_run is True
    recorder_cls.return_value.record.assert_not_called()


def test_get_artifacts_requires_active_session_for_real_downloads(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False, scheme="s3")
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetTransferResult(success=True, downloaded_files=[])
    recorder = MagicMock()
    recorder.record.side_effect = ValueError("No active session")

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
        patch("roar.application.get.service.LocalJobRecorder", return_value=recorder),
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        with pytest.raises(ValueError, match="No active session"):
            get_artifacts(_request(tmp_path))


def test_get_artifacts_creates_roar_git_tag(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False, scheme="s3")
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetTransferResult(success=True, downloaded_files=[])
    recorder = MagicMock()
    recorder.record.return_value = (42, "job-uid-1")

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.resolve_git_state") as resolve_git_state,
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
        patch("roar.application.get.service.LocalJobRecorder", return_value=recorder),
        patch(
            "roar.application.get.service.create_roar_git_tag",
            return_value=(True, None),
        ) as create_tag,
    ):
        resolve_git_state.return_value.commit = "deadbeef"
        response = get_artifacts(_request(tmp_path, tag=True))

    assert response.git_tag == "roar/deadbeef"
    create_tag.assert_called_once_with(tmp_path, "roar/deadbeef")


def test_get_artifacts_returns_flattened_response_shape(tmp_path: Path) -> None:
    parsed_source = MagicMock(is_prefix=False, scheme="s3")
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.get.return_value = GetTransferResult(
        success=False,
        error="boom",
        downloaded_files=[],
    )

    with (
        patch("roar.application.get.service.bootstrap"),
        patch("roar.application.get.service.parse_source", return_value=parsed_source),
        patch("roar.application.get.service.resolve_download_backend", return_value=MagicMock()),
        patch("roar.application.get.service.create_database_context", return_value=db_ctx),
        patch("roar.application.get.service.GetService", return_value=service),
    ):
        response = get_artifacts(_request(tmp_path))

    assert isinstance(response, GetResponse)
    assert response.success is False
    assert response.error == "boom"
