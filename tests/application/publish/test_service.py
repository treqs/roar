from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.requests import PutRequest, RegisterLineageRequest
from roar.application.publish.service import put_artifacts, register_lineage_target
from roar.application.publish.targets import ResolvedRegisterTarget
from roar.services.put.service import PutResult
from roar.services.registration.register_service import RegisterResult


def test_register_lineage_target_delegates_to_register_service(tmp_path: Path) -> None:
    expected = RegisterResult(success=True, session_hash="a" * 64)
    runtime = MagicMock()

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_glaas_url", return_value="http://localhost:3001"),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        mock_cls.return_value.register_artifact_lineage.return_value = expected

        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                dry_run=True,
            )
        )

    assert response.result is expected
    mock_cls.assert_called_once_with(
        glaas_client=runtime.glaas_client,
        lineage_collector=runtime.lineage_collector,
        coordinator=runtime.registration_coordinator,
        session_service=runtime.session_service,
    )
    mock_cls.return_value.register_artifact_lineage.assert_called_once_with(
        artifact_path="model.pt",
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        dry_run=True,
        as_blake3=False,
        skip_confirmation=False,
        confirm_callback=None,
    )


def test_register_lineage_target_dispatches_step_reference(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_glaas_url", return_value="http://localhost:3001"),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="step_reference", value="@4"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        mock_cls.return_value.register_step_lineage.return_value = RegisterResult(success=True)

        register_lineage_target(
            RegisterLineageRequest(
                target="@4",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    mock_cls.return_value.register_step_lineage.assert_called_once()


def test_register_lineage_target_dispatches_job_uid(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_glaas_url", return_value="http://localhost:3001"),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="job_uid", value="deadbeef"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        mock_cls.return_value.register_job_lineage.return_value = RegisterResult(success=True)

        register_lineage_target(
            RegisterLineageRequest(
                target="deadbeef",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    mock_cls.return_value.register_job_lineage.assert_called_once()


def test_put_artifacts_builds_put_service_and_creates_git_tag(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 1}
    put_result = PutResult(success=True, job_id=7, uploaded_files=[], dry_run=False)
    logger = MagicMock()
    git_state = MagicMock(commit="deadbeef", repo_root=tmp_path)
    runtime = MagicMock()
    backend = MagicMock()

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch("roar.application.publish.service._get_backend", return_value=backend),
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch("roar.application.publish.service.ensure_clean_publish_repo", return_value=git_state),
        patch(
            "roar.application.publish.service.create_publish_git_tag",
            return_value=(True, None),
        ),
    ):
        mock_put_cls.return_value.put.return_value = put_result

        response = put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
            )
        )

    mock_put_cls.assert_called_once()
    assert mock_put_cls.call_args.kwargs == {
        "db_context": db_ctx,
        "backend": backend,
        "destination": "memory://bucket/prefix",
        "repo_root": tmp_path,
        "roar_dir": tmp_path / ".roar",
        "glaas_client": runtime.glaas_client,
        "lineage_collector": runtime.lineage_collector,
        "registration_coordinator": runtime.registration_coordinator,
        "session_service": runtime.session_service,
    }
    mock_put_cls.return_value.put.assert_called_once_with(
        sources=["model.pt"],
        message="publish",
        dry_run=False,
        git_commit="deadbeef",
        git_tag="roar/deadbeef",
    )
    assert response.result is put_result
    assert response.git_tag == "roar/deadbeef"
    assert response.warnings == []


def test_put_artifacts_rejects_dirty_repo_before_put(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 1}

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch("roar.application.publish.service._get_backend", return_value=MagicMock()),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.ensure_clean_publish_repo",
            side_effect=ValueError("Repository has uncommitted changes"),
        ),
        pytest.raises(ValueError, match="Repository has uncommitted changes"),
    ):
        put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
            )
        )

    mock_put_cls.return_value.put.assert_not_called()


def test_put_artifacts_continues_when_git_preflight_warns(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 1}
    put_result = PutResult(success=True, job_id=3, uploaded_files=[], dry_run=False)

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch("roar.application.publish.service._get_backend", return_value=MagicMock()),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.ensure_clean_publish_repo",
            side_effect=RuntimeError("git unavailable"),
        ),
    ):
        mock_put_cls.return_value.put.return_value = put_result

        response = put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
            )
        )

    assert response.result is put_result
    assert response.git_tag is None
    assert response.warnings == ["Git operation failed: git unavailable"]
