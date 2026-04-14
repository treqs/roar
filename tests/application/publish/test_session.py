from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from roar.application.publish.session import PreparedPublishSession, prepare_publish_session
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext, SessionRegistrationResult


def _git_context() -> GitContext:
    return GitContext(repo="https://github.com/test/repo", commit="deadbeef", branch="main")


def _lineage() -> LineageData:
    return LineageData(
        jobs=[
            {
                "job_uid": "job-1",
                "command": "python train.py",
                "job_type": "run",
                "step_number": 1,
                "parent_job_uid": None,
                "_inputs": [{"artifact_hash": "input-1", "path": "data/input.csv"}],
                "_outputs": [{"artifact_hash": "output-1", "path": "models/output.bin"}],
                "metadata": {"runner": "python"},
            }
        ]
    )


def test_prepare_publish_session_computes_hash_without_registering(tmp_path: Path) -> None:
    glaas_client = MagicMock()
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    logger = MagicMock()

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=logger,
        register_with_glaas=False,
    )

    assert result == PreparedPublishSession(session_hash="session-hash")
    glaas_client.health_check.assert_not_called()
    session_service.register.assert_not_called()


def test_prepare_publish_session_uses_canonical_hash_when_lineage_and_creator_identity_are_provided(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    session_service = MagicMock()
    logger = MagicMock()

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=logger,
        register_with_glaas=False,
        lineage=_lineage(),
        creator_identity="treqs:user:user-123",
    )

    assert len(result.session_hash) == 64
    glaas_client.health_check.assert_not_called()
    session_service.compute_session_hash.assert_not_called()
    session_service.register.assert_not_called()


def test_prepare_publish_session_registers_with_glaas(tmp_path: Path) -> None:
    glaas_client = MagicMock()
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.register.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url="https://glaas.example/dag/session-hash",
    )
    logger = MagicMock()

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=logger,
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url="https://glaas.example/dag/session-hash",
    )
    glaas_client.health_check.assert_called_once()
    session_service.register.assert_called_once_with("session-hash", _git_context())


def test_prepare_publish_session_requires_configured_glaas(tmp_path: Path) -> None:
    glaas_client = MagicMock()
    glaas_client.is_configured.return_value = False
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"

    with pytest.raises(ValueError, match="GLaaS not configured"):
        prepare_publish_session(
            glaas_client=glaas_client,
            session_service=session_service,
            roar_dir=tmp_path / ".roar",
            session_id=7,
            git_context=_git_context(),
            logger=MagicMock(),
            register_with_glaas=True,
            configured_error="GLaaS not configured",
        )


def test_prepare_publish_session_surfaces_health_check_failures(tmp_path: Path) -> None:
    glaas_client = MagicMock()
    glaas_client.health_check.side_effect = RuntimeError("offline")
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"

    with pytest.raises(ValueError, match="GLaaS health check failed: offline"):
        prepare_publish_session(
            glaas_client=glaas_client,
            session_service=session_service,
            roar_dir=tmp_path / ".roar",
            session_id=7,
            git_context=_git_context(),
            logger=MagicMock(),
            register_with_glaas=True,
        )


def test_prepare_publish_session_surfaces_session_registration_failures(tmp_path: Path) -> None:
    glaas_client = MagicMock()
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.register.return_value = SessionRegistrationResult(
        success=False,
        session_hash="session-hash",
        error="rejected",
    )

    with pytest.raises(ValueError, match="Session registration failed: rejected"):
        prepare_publish_session(
            glaas_client=glaas_client,
            session_service=session_service,
            roar_dir=tmp_path / ".roar",
            session_id=7,
            git_context=_git_context(),
            logger=MagicMock(),
            register_with_glaas=True,
        )
