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


def test_prepare_publish_session_creates_registration_session_with_glaas(tmp_path: Path) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = "token-123"
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-123",
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
        session_url=None,
        registration_session_id="reg-session-123",
    )
    glaas_client.health_check.assert_called_once()
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode=None,
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_uses_remote_registry_publish_auth_when_legacy_client_not_passed(
    tmp_path: Path,
) -> None:
    remote_registry = MagicMock()
    remote_registry.session_service = MagicMock()
    remote_registry.session_service.compute_session_hash.return_value = "session-hash"
    remote_registry.session_service.create_registration_session.return_value = (
        SessionRegistrationResult(
            success=True,
            session_hash="session-hash",
            session_url=None,
            registration_session_id="reg-session-remote-123",
        )
    )
    remote_registry.publish_auth.access_token = "token-123"
    remote_registry.publish_auth.ssh_auth_available = False
    remote_registry.is_configured.return_value = True

    result = prepare_publish_session(
        remote_registry=remote_registry,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-remote-123",
    )
    remote_registry.health_check.assert_called_once()
    remote_registry.session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode=None,
    )
    remote_registry.session_service.register.assert_not_called()


def test_prepare_publish_session_creates_registration_session_with_ssh_only_auth(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = None
    glaas_client.publish_auth.ssh_auth_available = True
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-ssh-123",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-ssh-123",
    )
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode=None,
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_creates_registration_session_with_scoped_ssh_only_auth(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = {
        "owner_id": "owner-123",
        "owner_type": "organization",
        "project_id": "proj-123",
        "visibility": "private",
    }
    glaas_client.publish_auth.ssh_auth_available = True
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-scoped-ssh-123",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-scoped-ssh-123",
    )
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode=None,
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_creates_registration_session_with_delegated_auth(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = {
        "owner_id": "owner-123",
        "owner_type": "organization",
        "project_id": "proj-123",
        "visibility": "private",
    }
    glaas_client.publish_auth.ssh_auth_available = False
    glaas_client.publish_auth.delegated_auth_available = True
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-delegated-123",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-delegated-123",
    )
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode=None,
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_uses_anonymous_public_registration_sessions_when_supported(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = None
    glaas_client.publish_auth.ssh_auth_available = False
    glaas_client.health_check.return_value = {
        "status": "healthy",
        "features": {
            "registration_sessions": {
                "authenticated": True,
                "anonymous_public": True,
                "finalize_expected_hash": True,
                "finalize_server_authoritative_hash": True,
            }
        },
    }
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        registration_session_id="reg-session-anon-123",
        registration_session_mode="anonymous_public",
        registration_session_token="reg-token-123",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-anon-123",
        registration_session_mode="anonymous_public",
    )
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode="anonymous_public",
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_uses_anonymous_public_registration_sessions_when_ssh_auth_is_rejected(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = None
    glaas_client.publish_auth.ssh_auth_available = True
    glaas_client.probe_publish_auth.return_value = False
    glaas_client.health_check.return_value = {
        "status": "healthy",
        "features": {
            "registration_sessions": {
                "authenticated": True,
                "anonymous_public": True,
                "finalize_expected_hash": True,
                "finalize_server_authoritative_hash": True,
            }
        },
    }
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        registration_session_id="reg-session-anon-123",
        registration_session_mode="anonymous_public",
        registration_session_token="reg-token-123",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-anon-123",
        registration_session_mode="anonymous_public",
    )
    glaas_client.probe_publish_auth.assert_called_once_with()
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode="anonymous_public",
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_keeps_registration_session_when_ssh_auth_is_accepted(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = None
    glaas_client.publish_auth.ssh_auth_available = True
    glaas_client.probe_publish_auth.return_value = True
    glaas_client.health_check.return_value = {
        "status": "healthy",
        "features": {
            "registration_sessions": {
                "authenticated": True,
                "anonymous_public": True,
                "finalize_expected_hash": True,
                "finalize_server_authoritative_hash": True,
            }
        },
    }
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        registration_session_id="reg-session-ssh-123",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url=None,
        registration_session_id="reg-session-ssh-123",
    )
    glaas_client.probe_publish_auth.assert_called_once_with()
    session_service.create_registration_session.assert_called_once_with(
        client_session_id=None,
        mode=None,
    )
    session_service.register.assert_not_called()


def test_prepare_publish_session_falls_back_to_legacy_anonymous_registration_when_server_lacks_support(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    glaas_client.publish_auth.scope_request = None
    glaas_client.publish_auth.ssh_auth_available = False
    glaas_client.health_check.return_value = {
        "status": "healthy",
        "features": {
            "registration_sessions": {
                "authenticated": True,
                "anonymous_public": True,
                "finalize_expected_hash": False,
                "finalize_server_authoritative_hash": False,
            }
        },
    }
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.register.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url="https://glaas.example/dag/session-hash",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url="https://glaas.example/dag/session-hash",
        registration_session_id=None,
    )
    session_service.create_registration_session.assert_not_called()
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


def test_prepare_publish_session_surfaces_registration_session_creation_failures(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = "token-123"
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.create_registration_session.return_value = SessionRegistrationResult(
        success=False,
        session_hash="",
        error="rejected",
    )

    with pytest.raises(ValueError, match="Registration session creation failed: rejected"):
        prepare_publish_session(
            glaas_client=glaas_client,
            session_service=session_service,
            roar_dir=tmp_path / ".roar",
            session_id=7,
            git_context=_git_context(),
            logger=MagicMock(),
            register_with_glaas=True,
        )


def test_prepare_publish_session_uses_legacy_session_registration_without_access_token(
    tmp_path: Path,
) -> None:
    glaas_client = MagicMock()
    glaas_client.publish_auth.access_token = None
    session_service = MagicMock()
    session_service.compute_session_hash.return_value = "session-hash"
    session_service.register.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session-hash",
        session_url="https://glaas.example/dag/session-hash",
    )

    result = prepare_publish_session(
        glaas_client=glaas_client,
        session_service=session_service,
        roar_dir=tmp_path / ".roar",
        session_id=7,
        git_context=_git_context(),
        logger=MagicMock(),
        register_with_glaas=True,
    )

    assert result == PreparedPublishSession(
        session_hash="session-hash",
        session_url="https://glaas.example/dag/session-hash",
        registration_session_id=None,
    )
    session_service.create_registration_session.assert_not_called()
    session_service.register.assert_called_once_with("session-hash", _git_context())
