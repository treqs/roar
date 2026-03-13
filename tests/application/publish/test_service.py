from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.register_preparation import PreparedRegisterExecution
from roar.application.publish.requests import PutRequest, RegisterLineageRequest
from roar.application.publish.results import PutResponse, RegisterLineageResponse
from roar.application.publish.service import put_artifacts, register_lineage_target
from roar.application.publish.targets import ResolvedRegisterTarget
from roar.core.interfaces.lineage import LineageData
from roar.services.put.service import PutResult
from roar.services.registration.register_service import RegisterResult


def test_register_lineage_target_collects_and_registers(tmp_path: Path) -> None:
    expected = RegisterResult(success=True, session_hash="a" * 64)
    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7})
    collected.session_id = 7
    collected.artifact_hash = "a" * 64
    collected.session_hash_override = None
    prepared = PreparedRegisterExecution(
        git_context=MagicMock(),
        session_id=7,
        session_hash="a" * 64,
        session_url="https://glaas.local/dag/session",
        git_tag_name=None,
        git_tag_repo_root=None,
    )

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch(
            "roar.application.publish.service.get_glaas_url",
            return_value="http://localhost:3001",
        ),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ) as collect_lineage,
        patch(
            "roar.application.publish.service.prepare_register_execution",
            return_value=prepared,
        ) as prepare_register,
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        mock_cls.return_value.register_prepared_lineage.return_value = expected

        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                dry_run=True,
            )
        )

    assert response == RegisterLineageResponse(success=True, session_hash="a" * 64)
    mock_cls.assert_called_once_with(
        glaas_client=runtime.glaas_client,
        coordinator=runtime.registration_coordinator,
    )
    collect_lineage.assert_called_once_with(
        target=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        lineage_collector=runtime.lineage_collector,
        session_service=runtime.session_service,
        logger=logger,
    )
    prepare_register.assert_called_once_with(
        runtime=runtime,
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        session_id=7,
        dry_run=True,
        session_hash_override=None,
        logger=logger,
    )
    mock_cls.return_value.register_prepared_lineage.assert_called_once_with(
        lineage=collected.lineage,
        roar_dir=tmp_path / ".roar",
        artifact_hash="a" * 64,
        dry_run=True,
        as_blake3=False,
        skip_confirmation=False,
        confirm_callback=None,
        prepared=prepared,
    )


def test_register_lineage_target_returns_collection_error(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch(
            "roar.application.publish.service.get_glaas_url",
            return_value="http://localhost:3001",
        ),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="missing.csv"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(None, "File not found: missing.csv"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        response = register_lineage_target(
            RegisterLineageRequest(
                target="missing.csv",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    assert response == RegisterLineageResponse(success=False, error="File not found: missing.csv")
    mock_cls.return_value.register_prepared_lineage.assert_not_called()


def test_register_lineage_target_returns_preparation_error(tmp_path: Path) -> None:
    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7})
    collected.session_id = 7
    collected.artifact_hash = "a" * 64
    collected.session_hash_override = None

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.get_glaas_url",
            return_value="http://localhost:3001",
        ),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ),
        patch(
            "roar.application.publish.service.prepare_register_execution",
            side_effect=ValueError("GLaaS not configured"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    assert response == RegisterLineageResponse(
        success=False,
        artifact_hash="a" * 64,
        error="GLaaS not configured",
    )
    mock_cls.return_value.register_prepared_lineage.assert_not_called()


def test_register_lineage_target_creates_git_tag_after_success(tmp_path: Path) -> None:
    expected = RegisterResult(success=True, session_hash="a" * 64)
    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7})
    collected.session_id = 7
    collected.artifact_hash = "a" * 64
    collected.session_hash_override = None
    prepared = PreparedRegisterExecution(
        git_context=MagicMock(),
        session_id=7,
        session_hash="a" * 64,
        session_url="https://glaas.local/dag/session",
        git_tag_name="roar/deadbeef",
        git_tag_repo_root=tmp_path,
    )

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.get_glaas_url",
            return_value="http://localhost:3001",
        ),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ),
        patch(
            "roar.application.publish.service.prepare_register_execution",
            return_value=prepared,
        ),
        patch("roar.application.publish.service.finalize_register_git") as finalize_register,
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        mock_cls.return_value.register_prepared_lineage.return_value = expected

        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    assert response == RegisterLineageResponse(success=True, session_hash="a" * 64)
    finalize_register.assert_called_once_with(
        result_success=True,
        dry_run=False,
        git_tag_name="roar/deadbeef",
        git_tag_repo_root=tmp_path,
        logger=logger,
    )


def test_put_artifacts_builds_put_service_and_creates_git_tag(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    put_result = PutResult(success=True, job_id=7, uploaded_files=[], dry_run=False)
    logger = MagicMock()
    runtime = MagicMock()
    backend = MagicMock()
    prepared = MagicMock()
    prepared_git = MagicMock(git_commit="deadbeef", expected_tag="roar/deadbeef", warnings=())

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend",
            return_value=backend,
        ),
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch(
            "roar.application.publish.service.prepare_put_execution",
            return_value=prepared,
        ) as prepare_put,
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.prepare_put_git",
            return_value=prepared_git,
        ),
        patch(
            "roar.application.publish.service.finalize_put_git",
            return_value=("roar/deadbeef", []),
        ),
    ):
        mock_put_cls.return_value.put_prepared.return_value = put_result

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
        "lineage_collector": runtime.lineage_collector,
        "registration_coordinator": runtime.registration_coordinator,
    }
    prepare_put.assert_called_once_with(
        db_ctx=db_ctx,
        runtime=runtime,
        roar_dir=tmp_path / ".roar",
        repo_root=tmp_path,
        sources=["model.pt"],
        destination="memory://bucket/prefix",
        git_commit="deadbeef",
        logger=logger,
    )
    mock_put_cls.return_value.put_prepared.assert_called_once_with(
        prepared=prepared,
        sources=["model.pt"],
        message="publish",
        dry_run=False,
        git_commit="deadbeef",
        git_tag="roar/deadbeef",
    )
    assert response == PutResponse(
        success=True,
        destination="memory://bucket/prefix",
        job_id=7,
        dry_run=False,
        git_tag="roar/deadbeef",
        warnings=[],
    )


def test_put_artifacts_rejects_dirty_repo_before_put(tmp_path: Path) -> None:
    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(MagicMock()),
        ),
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend",
            return_value=MagicMock(),
        ),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.prepare_put_git",
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

    mock_put_cls.return_value.put_prepared.assert_not_called()


def test_put_artifacts_continues_when_git_preflight_warns(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    put_result = PutResult(success=True, job_id=3, uploaded_files=[], dry_run=False)
    prepared = MagicMock()
    prepared_git = MagicMock(git_commit=None, expected_tag=None, warnings=("Git operation failed: git unavailable",))

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend",
            return_value=MagicMock(),
        ),
        patch(
            "roar.application.publish.service.prepare_put_execution",
            return_value=prepared,
        ),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.prepare_put_git",
            return_value=prepared_git,
        ),
        patch(
            "roar.application.publish.service.finalize_put_git",
            return_value=(None, []),
        ),
    ):
        mock_put_cls.return_value.put_prepared.return_value = put_result

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

    assert response == PutResponse(
        success=True,
        destination="memory://bucket/prefix",
        job_id=3,
        dry_run=False,
        warnings=["Git operation failed: git unavailable"],
    )


def test_put_artifacts_returns_preparation_error_before_service(tmp_path: Path) -> None:
    db_ctx = MagicMock()

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend",
            return_value=MagicMock(),
        ),
        patch(
            "roar.application.publish.service.prepare_put_git",
            return_value=MagicMock(git_commit=None, expected_tag=None, warnings=()),
        ),
        patch("roar.application.publish.service.build_publish_runtime", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.prepare_put_execution",
            side_effect=ValueError("No active session"),
        ),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        pytest.raises(ValueError, match="No active session"),
    ):
        put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
                dry_run=True,
            )
        )

    mock_put_cls.return_value.put_prepared.assert_not_called()
